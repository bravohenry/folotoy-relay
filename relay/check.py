"""
[INPUT]: codec/server 实现、设备消息结构和本机临时 WebSocket 端口。
[OUTPUT]: 编解码已知向量、双客户端转发、握手长度与非法消息拒绝的可运行检查。
[POS]: relay 的最小协议验收，不访问外部中继，不把模拟客户端视为硬件。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""
import asyncio
import json
import struct
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from codec import decode, encode, validate
from server import GROUP, Relay, text

A, B, C = "001122334455", "aabbccddeeff", "102030405060"


def hello(mac, name="伙伴") -> str:
    return text({"type": "hello", "group": GROUP, "mac": mac, "name": name, "avatar": 1})


async def receive(socket):
    message = await asyncio.wait_for(socket.recv(), 2)
    return json.loads(message) if isinstance(message, str) else message


def codec_check() -> None:
    silent = bytes(84)
    assert decode(silent) == (0,) * 160
    packet = struct.pack("<hBB", 1000, 0, 0) + bytes([0x19]) + bytes(79)
    assert decode(packet)[:4] == (999, 1000, 1000, 1000)
    # 头部 predictor 是帧前状态，不额外输出为第一个采样。
    encoded, state = encode([1000] * 160, (1000, 0))
    assert encoded == struct.pack("<hBB", 1000, 0, 0) + bytes(80)
    assert state == (1000, 0)
    saturated = struct.pack("<hBB", 32760, 88, 0) + bytes([0x07]) + bytes(79)
    assert decode(saturated)[0] == 32767
    for invalid in (bytes(83), b"\0\0\x59\0" + bytes(80), b"\0\0\0\1" + bytes(80)):
        try:
            validate(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("无效帧未被拒绝")


async def protocol_check() -> None:
    # 默认新固件分组有 12 字节；不再受早期补丁的 11 字节限制。
    assert Relay({A, B}, "sidecar-home").group == "sidecar-home"
    assert Relay({A}, "a" * 31).group == "a" * 31
    for invalid in ("", "a" * 32, "bad\n", "伙伴" * 11):
        try:
            Relay({A}, invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("非法分组被接受")
    relay = Relay({A, B})
    async with serve(relay.handle, "127.0.0.1", 0, compression=None, ping_interval=None,
                     max_size=512, max_queue=4, server_header=None) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/ws"
        # 无扩展握手；设备响应读取缓冲区只有 256 字节。
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(("GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        assert b"101 Switching Protocols" in response and len(response) < 256
        writer.close()
        await writer.wait_closed()
        async with connect(url, compression=None, proxy=None) as first:
            await first.send(hello(A))
            assert len((await receive(first))["peers"]) == 1
            async with connect(url, compression=None, proxy=None) as second:
                await second.send(hello(B, "另一台"))
                assert len((await receive(first))["peers"]) == 2
                assert len((await receive(second))["peers"]) == 2
                await first.send(text({"type": "ptt-start", "from": {"mac": C}}))
                started = await receive(second)
                assert started["type"] == "ptt-start" and started["from"]["mac"] == A
                await first.send(bytes(84))
                assert await receive(second) == bytes.fromhex(A) + bytes(84)
                await first.send(text({"type": "ptt-stop"}))
                assert (await receive(second))["type"] == "ptt-stop"
                await first.send(text({"type": "find-start"}))
                assert (await receive(second))["type"] == "find-start"
                await second.send(text({"type": "find-ack", "to": A}))
                ack = await receive(first)
                assert ack["type"] == "find-ack" and ack["from"]["mac"] == B
                await second.send(text({"type": []}))
                try:
                    await second.recv()
                except ConnectionClosed as error:
                    assert error.rcvd.code == 1008
                else:
                    raise AssertionError("非法控制未关闭连接")
            assert len((await receive(first))["peers"]) == 1
            async with connect(url, compression=None, proxy=None) as rejected:
                await rejected.send(hello(C))
                try:
                    await rejected.recv()
                except ConnectionClosed as error:
                    assert error.rcvd.code == 1008
                else:
                    raise AssertionError("未允许的 MAC 被接受")
            await first.send(bytes(84))
            try:
                await first.recv()
            except ConnectionClosed as error:
                assert error.rcvd.code == 1008
            else:
                raise AssertionError("未按下说话时音频被接受")
    # 等服务端处理器结束后检查清理，不能用客户端 Close 到达替代 finally 完成。
    assert not relay.peers
    print("PASS: IMA 已知向量、握手、在线状态、控制/音频转发和非法消息拒绝")


if __name__ == "__main__":
    codec_check()
    asyncio.run(protocol_check())
