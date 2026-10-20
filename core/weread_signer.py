"""WeRead refresh signature, adapted from x554960766/wechat-mp-tools.

MIT License

Copyright (c) 2026 xuyi

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

DISCLAIMER: This software is provided for educational and personal research purposes only. The author does not assume any responsibility for unauthorized access, data loss, or system issues arising from the use of this tool. Use it at your own risk.
"""

import hashlib

# libencrypt.so 内置 256 字节 S-Box 置换盒 (偏移 0xd9fc)
SBOX = [
    52, 202, 85, 64, 29, 182, 147, 198, 49, 48, 41, 53, 50, 167, 184, 17,
    194, 181, 22, 250, 139, 177, 36, 164, 16, 144, 4, 233, 8, 248, 59, 138,
    156, 140, 68, 249, 188, 92, 105, 226, 161, 218, 210, 211, 117, 137, 247, 30,
    45, 80, 86, 215, 114, 83, 191, 34, 251, 32, 15, 1, 46, 69, 135, 110,
    102, 72, 242, 224, 205, 254, 103, 169, 67, 244, 148, 81, 206, 165, 74, 238,
    19, 38, 142, 204, 170, 51, 20, 93, 14, 57, 187, 207, 145, 43, 129, 77,
    234, 153, 236, 26, 44, 133, 197, 217, 54, 116, 75, 24, 225, 241, 61, 157,
    65, 159, 180, 23, 13, 214, 76, 190, 220, 175, 151, 40, 119, 240, 98, 255,
    113, 193, 200, 39, 143, 108, 104, 168, 155, 230, 89, 28, 27, 18, 9, 152,
    78, 63, 6, 55, 0, 186, 31, 10, 25, 47, 201, 213, 208, 87, 73, 111,
    253, 37, 228, 97, 12, 66, 203, 150, 100, 95, 219, 173, 96, 35, 141, 154,
    109, 195, 196, 94, 62, 185, 146, 106, 189, 91, 7, 127, 118, 149, 237, 79,
    171, 132, 122, 128, 231, 120, 199, 229, 235, 115, 131, 107, 252, 56, 70, 125,
    71, 101, 179, 82, 99, 58, 5, 209, 239, 163, 166, 222, 158, 60, 2, 174,
    178, 123, 160, 246, 243, 42, 192, 172, 134, 3, 90, 84, 11, 245, 130, 212,
    126, 227, 223, 176, 216, 221, 33, 232, 124, 136, 162, 121, 88, 112, 183, 21
]

# libencrypt.so 偏移 0xdafc 处的固定私钥盐
SALT = b"5a6f1"


def remap_string(s: str) -> bytes:
    """weread::RemapString"""
    raw_bytes = s.encode("utf-8")
    return bytes(SBOX[b] for b in raw_bytes)


def gen_signature(str_list: list) -> str:
    """
    纯 Python 复刻 Java_weread_encrypt_EncryptUtils_nativeGetSignatures
    """
    # 1. 外部字符串逐个通过 SBOX 字节置换
    remapped_items = [remap_string(s) for s in str_list]

    # 2. 插入内置私钥盐值
    remapped_items.append(SALT)

    # 3. 按 C++ std::string 字节序字典升序排序 (std::sort)
    remapped_items.sort()

    # 4. 拼接
    joined = b"".join(remapped_items)
    n = len(joined)
    if n == 0:
        return ""

    # 5. 第一轮异或累加和与循环移位
    xor1 = 0
    for b in joined:
        xor1 ^= b
    shift1 = xor1 % 11

    shifted1 = bytearray(n)
    for i in range(n):
        shifted1[(i + shift1) % n] = joined[i]

    # 6. 第一轮 SHA256 并格式化为 64 字节小写 hex
    hash1 = hashlib.sha256(shifted1).hexdigest().encode("ascii")

    # 7. 第二轮异或累加和与循环移位
    n2 = len(hash1)  # 64
    xor2 = 0
    for b in hash1:
        xor2 ^= b
    shift2 = xor2 % 11

    shifted2 = bytearray(n2)
    for i in range(n2):
        shifted2[(i + shift2) % n2] = hash1[i]

    # 8. 第二轮 SHA256 最终签名
    signature = hashlib.sha256(shifted2).hexdigest()
    return signature

