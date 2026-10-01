"""
[INPUT]: 已还原的 /ws 设备协议、websockets 库和显式设备 MAC 允许列表。
[OUTPUT]: 本地兼容中继，转发伙伴状态、对讲控制、寻呼和带发送者地址的音频。
[POS]: relay 的运行入口，只实现已有客户端格式；MAC 允许列表不等同身份认证。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""
import argparse
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from codec import FRAME_BYTES, validate

GROUP = "sidecar-home"
MAX_CLIENTS = 5  # 设备最多显示另外四个伙伴，接收文本缓冲区为 512 字节。
CONTROL_TYPES = {"ptt-start", "ptt-stop", "find-start", "find-stop", "find-ack"}
MAC_PATTERN = re.compile(r"[0-9a-f]{12}\Z")
LOGGER = logging.getLogger("sidecar")


def text(message: dict) -> str:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 511:
        raise ValueError("消息超出设备 511 字节有效文本容量")
    return encoded


def identity(message: dict, group: str = GROUP) -> dict:
    if message.get("type") != "hello" or message.get("group") != group:
        raise ValueError("需要设备 hello 和匹配的组名")
    mac, name, avatar = (message.get(key) for key in ("mac", "name", "avatar"))
    if not isinstance(mac, str) or not MAC_PATTERN.fullmatch(mac):
        raise ValueError("MAC 格式错误")
    if (not isinstance(name, str) or not 1 <= len(name.encode()) <= 15
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError("名称须为 1..15 个 UTF-8 字节")
    if type(avatar) is not int or not 0 <= avatar <= 9:
        raise ValueError("头像编号须为 0..9")
    return {"mac": mac, "name": name, "avatar": avatar}


def parse(message) -> dict:
    if not isinstance(message, str):
        raise ValueError("需要 JSON 文本消息")
    try:
        result = json.loads(message)
    except (ValueError, RecursionError) as error:
        raise ValueError("JSON 解析失败") from error
    if not isinstance(result, dict):
        raise ValueError("消息须为 JSON 对象")
    return result


@dataclass
class Peer:
    socket: object
    identity: dict
    talking: bool = False


class Relay:
    def __init__(self, allowed_macs: set[str], group: str = GROUP):
        if not allowed_macs or any(not MAC_PATTERN.fullmatch(mac) for mac in allowed_macs):
            raise ValueError("必须显式指定格式正确的设备 MAC 允许列表")
        self.allowed_macs = allowed_macs
        if (not isinstance(group, str) or not 1 <= len(group.encode()) <= 31
                or any(ord(char) < 32 or ord(char) == 127 for char in group)):
            raise ValueError("组名须为 1..31 个 UTF-8 字节，且不含控制字符")
        self.group = group
        self.peers: dict[str, Peer] = {}

    async def send(self, peer: Peer, message) -> None:
        try:
            await peer.socket.send(message)
        except ConnectionClosed:
            pass  # 连接处理器的 finally 负责清理伙伴与发布离线状态。

    async def presence(self) -> None:
        peers = list(self.peers.values())
        message = text({"type": "presence", "peers": [peer.identity for peer in peers]})
        await asyncio.gather(*(self.send(peer, message) for peer in peers))

    async def broadcast(self, sender: Peer, message, target: str | None = None) -> None:
        recipients = [peer for mac, peer in self.peers.items()
                      if peer is not sender and (target is None or mac == target)]
        await asyncio.gather(*(self.send(peer, message) for peer in recipients))

    async def handle(self, socket) -> None:
        peer = None
        try:
            if socket.request.path != "/ws":
                raise ValueError("仅接受 /ws")
            hello = parse(await asyncio.wait_for(socket.recv(), timeout=5))
            who = identity(hello, self.group)
            mac = who["mac"]
            if mac not in self.allowed_macs or mac in self.peers:
                raise ValueError("设备不在允许列表或已经在线")
            if len(self.peers) >= MAX_CLIENTS:
                raise ValueError("已达到设备伙伴容量")
            peer = Peer(socket, who)
            self.peers[mac] = peer
            LOGGER.info("设备上线：%s", mac)
            await self.presence()
            async for message in socket:
                if isinstance(message, bytes):
                    validate(message)
                    if not peer.talking:
                        raise ValueError("音频须在 ptt-start 后发送")
                    await self.broadcast(peer, bytes.fromhex(mac) + message)
                    continue
                control = parse(message)
                kind = control.get("type")
                if not isinstance(kind, str) or kind not in CONTROL_TYPES:
                    raise ValueError("不支持的控制消息")
                target = None
                if kind == "find-ack":
                    target = control.get("to")
                    if not isinstance(target, str) or not MAC_PATTERN.fullmatch(target):
                        raise ValueError("寻呼回复须含有效 to 地址")
                if kind == "ptt-start":
                    peer.talking = True
                elif kind == "ptt-stop":
                    peer.talking = False
                # from 由已登记连接生成，不采用客户端传入的发送者。
                await self.broadcast(peer, text({"type": kind, "from": who}), target)
        except (ValueError, asyncio.TimeoutError) as error:
            LOGGER.info("拒绝连接：%s", error)
            await socket.close(code=1008, reason="Invalid device protocol")
        except ConnectionClosed:
            pass
        finally:
            if peer is not None:
                del self.peers[peer.identity["mac"]]
                if peer.talking:
                    await self.broadcast(peer, text({"type": "ptt-stop", "from": peer.identity}))
                LOGGER.info("设备离线：%s", peer.identity["mac"])
                await self.presence()

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(15)  # 原设备收到 45 秒静默就重连；不依赖服务端 Ping 实现。
            await self.presence()


async def run(host: str, port: int, macs: set[str], group: str = GROUP) -> None:
    relay = Relay(macs, group)
    # 禁用压缩，保证响应头小于原设备 256 字节握手缓冲区。
    async with serve(relay.handle, host, port, compression=None, ping_interval=None,
                     max_size=512, max_queue=4, server_header=None):
        LOGGER.info("兼容中继监听 %s:%d；MAC 允许列表不提供密码学认证", host, port)
        await relay.heartbeat()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sidecar 原设备协议兼容中继")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7300)
    parser.add_argument("--allow-mac", action="append", required=True)
    parser.add_argument("--group", default=GROUP)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run(args.host, args.port, {mac.lower() for mac in args.allow_mac}, args.group))


if __name__ == "__main__":
    main()
