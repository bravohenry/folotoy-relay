"""
[INPUT]: 84 字节 IMA ADPCM 帧，或 160 个有符号 PCM16 单声道采样。
[OUTPUT]: 与设备低半字节优先、独立帧头格式一致的音频编码和解码函数。
[POS]: relay 的音频格式实现，不参与中继连接管理。
[PROTOCOL]: 变更时更新此头部，然后检查 AGENTS.md
"""
import struct

FRAME_BYTES = 84
SAMPLES_PER_FRAME = 160
SAMPLE_RATE = 8000
INDEX_DELTA = (-1, -1, -1, -1, 2, 4, 6, 8) * 2
STEP = (7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
        34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130,
        143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449,
        494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411,
        1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660,
        4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493,
        10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385,
        24623, 27086, 29794, 32767)


def validate(frame: bytes) -> None:
    if len(frame) != FRAME_BYTES or frame[2] > 88 or frame[3] != 0:
        raise ValueError("音频帧须为 84 字节，步长索引 0..88，保留字节为 0")


def update(predictor: int, index: int, nibble: int) -> tuple[int, int]:
    step = STEP[index]
    delta = step >> 3
    if nibble & 4:
        delta += step
    if nibble & 2:
        delta += step >> 1
    if nibble & 1:
        delta += step >> 2
    predictor += -delta if nibble & 8 else delta
    return max(-32768, min(32767, predictor)), max(0, min(88, index + INDEX_DELTA[nibble]))


def decode(frame: bytes) -> tuple[int, ...]:
    validate(frame)
    predictor, index, _ = struct.unpack_from("<hBB", frame)
    samples = []
    for packed in frame[4:]:
        for nibble in (packed & 15, packed >> 4):
            predictor, index = update(predictor, index, nibble)
            samples.append(predictor)
    return tuple(samples)


def encode(samples, state: tuple[int, int] = (0, 0)) -> tuple[bytes, tuple[int, int]]:
    samples = tuple(samples)
    predictor, index = state
    if len(samples) != SAMPLES_PER_FRAME:
        raise ValueError("一帧须有 160 个 PCM16 采样")
    if (type(predictor) is not int or type(index) is not int
            or not -32768 <= predictor <= 32767 or not 0 <= index <= 88):
        raise ValueError("编码状态超出范围")
    if any(type(sample) is not int or not -32768 <= sample <= 32767 for sample in samples):
        raise ValueError("PCM16 采样须为 -32768..32767 的整数")
    frame = bytearray(struct.pack("<hBB", predictor, index, 0) + bytes(80))
    for position, sample in enumerate(samples):
        difference = sample - predictor
        nibble = 8 if difference < 0 else 0
        remainder = abs(difference)
        step = STEP[index]
        for flag, threshold in ((4, step), (2, step >> 1), (1, step >> 2)):
            if remainder >= threshold:
                nibble |= flag
                remainder -= threshold
        frame[4 + position // 2] |= nibble << (4 * (position % 2))
        predictor, index = update(predictor, index, nibble)
    return bytes(frame), (predictor, index)
