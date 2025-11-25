import os
import sys

def analyze_photo(path):
    print(f"--- 分析文件: {path} ---")
    if not os.path.exists(path):
        print("❌ 文件不存在")
        return

    file_size = os.path.getsize(path)
    print(f"文件大小: {file_size / 1024 / 1024:.2f} MB")

    with open(path, 'rb') as f:
        data = f.read()

    # 1. 寻找 JPEG 结束标记 (FF D9)
    # 注意：FF D9 可能在文件中出现多次（缩略图里也有），我们要找位于文件后半部分的，或者逻辑上最后的一个
    # 但更靠谱的是找 MP4 的头 (ftyp)

    # 2. 寻找 MP4 签名 (ftyp)
    # MP4 box 结构: [4字节长度] [4字节类型='ftyp'] ...
    # 所以我们搜索 b'ftyp'，然后向前推4个字节就是视频开始

    # 我们搜索所有的 ftyp 出现位置
    offset = 0
    found_video = False

    while True:
        # 在 data[offset:] 中搜索 ftyp
        idx = data.find(b'ftyp', offset)
        if idx == -1:
            break

        # 视频开始位置通常是 ftyp 前面 4 个字节
        video_start = idx - 4

        # 验证这前4个字节是否像是一个长度数值
        # MP4 header 长度通常不大，比如 20-40 字节 (Hex: 00 00 00 18 等)
        if video_start >= 0:
            potential_len = int.from_bytes(data[video_start:video_start+4], 'big')
            print(f"发现 potential video at offset: {video_start} (Hex: {video_start:X})")
            print(f"  Header length value: {potential_len}")

            # 简单的启发式判断：如果这个位置在文件后半部分，且长度合理，极大概率是 Live Video
            if video_start > file_size * 0.3 and 16 < potential_len < 256:
                print("  ✅ 看起来像是真正的视频开始位置！")
                print(f"  视频大小: {(file_size - video_start) / 1024 / 1024:.2f} MB")

                # 尝试读取前几个字节看看是不是 H.265
                # H.265 对应的品牌通常是 hevc, hev1, hvc1
                # H.264 对应的品牌通常是 avc1, mp41, isom
                brand = data[idx+4 : idx+8].decode('utf-8', errors='ignore')
                print(f"  视频编码标识 (Brand): {brand}")

                if brand in ['hvc1', 'hev1', 'hevc']:
                    print("  ⚠️ 警告: 视频似乎是 H.265 (HEVC) 格式。")
                    print("  浏览器可能无法直接播放！请尝试在手机设置中将相机格式改为 '兼容性优先(H.264)'。")

                found_video = True

        offset = idx + 4

    if not found_video:
        print("❌ 未检测到嵌入的视频流。这可能是一张普通 JPG，或者微信压缩过的图片。")

if __name__ == "__main__":
    # 把你的原始照片路径填在这里
    target_file = r"D:\live测试\test3.jpg"
    # if len(sys.argv) > 1:
    #     target_file = sys.argv[1]
    # else:
    #     target_file = input("请输入原始图片路径: ").strip().replace('"', '')

    analyze_photo(target_file)