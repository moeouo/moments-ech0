import sqlite3
import time
import os
import shutil
import binascii
from datetime import datetime
import json

# 配置路径
SOURCE_DB_PATH = "XX/db.sqlite" #moments的db文件路径
TARGET_DB_PATH = "XX/ech0.db" #ech0的db文件路径
SOURCE_UPLOAD_DIR = "XX/upload" #moments的上传文件路径
TARGET_UPLOAD_DIR = "XX/files" #ech0的上传文件路径
TARGET_USERNAME = "XX" #你在ech0中的用户名

def generate_uuid7(timestamp_ms=None):
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    ts_bytes = timestamp_ms.to_bytes(6, byteorder='big')
    rand_bytes = bytearray(os.urandom(10))
    # Set version to 7
    rand_bytes[0] = (rand_bytes[0] & 0x0F) | 0x70
    # Set variant to 10xx
    rand_bytes[2] = (rand_bytes[2] & 0x3F) | 0x80
    uuid_bytes = ts_bytes + rand_bytes
    hex_str = binascii.hexlify(uuid_bytes).decode('ascii')
    return f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"

def parse_time(time_str):
    # 解析类似 "2023-01-01 12:00:00" 的时间
    try:
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp())
    except ValueError:
        return int(time.time())

def main():
    if not os.path.exists(SOURCE_DB_PATH):
        print(f"[-] 找不到源数据库文件: {SOURCE_DB_PATH}")
        return

    if not os.path.exists(TARGET_DB_PATH):
        print(f"[-] 找不到目标数据库文件: {TARGET_DB_PATH}")
        print("[-] 请先启动一次 Ech0 程序，以完成数据库和表的初始化！")
        return

    # 确保目标文件目录存在
    os.makedirs(TARGET_UPLOAD_DIR, exist_ok=True)

    # 连接数据库
    source_conn = sqlite3.connect(SOURCE_DB_PATH)
    target_conn = sqlite3.connect(TARGET_DB_PATH)
    
    source_cursor = source_conn.cursor()
    target_cursor = target_conn.cursor()

    # 检查 Creator 用户是否存在
    target_cursor.execute("SELECT id FROM users WHERE username = ?", (TARGET_USERNAME,))
    user_row = target_cursor.fetchone()
    if not user_row:
        print(f"[-] 在 Ech0 数据库中找不到用户: {TARGET_USERNAME}")
        print(f"[-] 请先在 Ech0 中创建该用户。")
        return
    
    creator_id = user_row[0]
    creator_uid8 = creator_id.replace('-', '')[:8].lower()

    print(f"[+] 找到目标用户 {TARGET_USERNAME} (ID: {creator_id})")

    # 读取旧数据
    source_cursor.execute("SELECT id, content, imgs, createdAt FROM Memo ORDER BY createdAt ASC")
    memos = source_cursor.fetchall()

    success_count = 0
    fail_count = 0

    print(f"[+] 开始迁移，共计 {len(memos)} 条动态...")

    # 定义路由规则，与 Ech0 原生 schema.go 保持一致
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif"}
    audio_exts = {".mp3", ".flac", ".wav", ".m4a", ".ogg"}
    video_exts = {".mp4", ".avi", ".mkv", ".webm"}
    doc_exts = {".pdf", ".doc", ".docx"}

    def resolve_path(filename):
        ext = os.path.splitext(filename)[1].lower()
        if ext in image_exts:
            return "images"
        elif ext in audio_exts:
            return "audios"
        elif ext in video_exts:
            return "videos"
        elif ext in doc_exts:
            return "documents"
        return "files"

    for row in memos:
        memo_id, content, imgs_str, created_at_str = row
        if not content and not imgs_str:
            continue

        ts_seconds = parse_time(created_at_str)
        ts_ms = ts_seconds * 1000

        # 生成动态的 UUIDv7
        echo_id = generate_uuid7(ts_ms)

        try:
            # 插入动态
            target_cursor.execute("""
                INSERT INTO echos (id, content, username, layout, private, user_id, fav_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (echo_id, content or "", TARGET_USERNAME, "waterfall", 0, creator_id, 0, ts_seconds))

            # 处理图片
            if imgs_str and imgs_str.strip():
                img_paths = [p.strip() for p in imgs_str.split(',') if p.strip()]
                
                # 过滤缩略图等未被直接声明为正图的图片。
                # 观察到你的原数据库里 `upload` 目录下很多 `_thumb` 的缩略图，且某些情况下可能会出现多余文件
                # 如果你的原程序里，图片并不是保存在 content 里（比如只展示在瀑布流/九宫格），那么通过 content 过滤会导致所有图片丢失。
                # 由于你说“有些没在正文用过的图片被搬走了”，可能指的是：
                # 1. 没有清理干净的脏数据（在imgs字段里，但实际已经废弃）
                # 2. 或者 content 中包含了 ![img](/upload/xxx.jpg) 的 Markdown 语法。
                
                # 判断策略：如果 content 中有包含图片路径，则以 content 中出现的为准；
                # 如果 content 中没有任何图片路径（即全都是九宫格附图），那么就不进行强制过滤，
                # 但过滤掉结尾带 `_thumb` 的缩略图，避免它们被当成新图片重新迁移一次。
                valid_img_paths = []
                content_has_imgs = "/upload/" in (content or "")
                
                for p in img_paths:
                    if "_thumb" in p:
                        continue # 坚决不要缩略图
                        
                    if content_has_imgs:
                        if p in (content or ""):
                            valid_img_paths.append(p)
                    else:
                        valid_img_paths.append(p)
                
                for sort_order, img_path in enumerate(valid_img_paths):
                    original_filename = os.path.basename(img_path)
                    if not original_filename:
                        continue

                    src_file = os.path.join("tran", img_path.lstrip('/'))
                    if not os.path.exists(src_file):
                        src_file = os.path.join(SOURCE_UPLOAD_DIR, original_filename)
                        if not os.path.exists(src_file):
                            print(f"    [!] 找不到图片文件: {img_path}，跳过该图片。")
                            continue

                    ext = os.path.splitext(original_filename)[1].lower()
                    if not ext:
                        ext = ".bin"
                    
                    rand_hex = binascii.hexlify(os.urandom(4)).decode('ascii')
                    new_key = f"{creator_uid8}_{ts_seconds}_{rand_hex}{ext}"

                    # 按照原生路由逻辑获取前缀文件夹
                    folder_prefix = resolve_path(new_key)
                    
                    # 对应的 fileCategory
                    category_map = {
                        "images": "image",
                        "audios": "audio",
                        "videos": "video",
                        "documents": "document",
                        "files": "file"
                    }
                    file_category = category_map.get(folder_prefix, "image")
                    
                    # 拷贝文件（确保前缀目录存在）
                    target_folder = os.path.join(TARGET_UPLOAD_DIR, folder_prefix)
                    os.makedirs(target_folder, exist_ok=True)
                    dst_file = os.path.join(target_folder, new_key)
                    shutil.copy2(src_file, dst_file)

                    file_id = generate_uuid7(ts_ms + sort_order)
                    # 原生 URL 逻辑也会带上路由前缀
                    file_url = f"/api/files/{folder_prefix}/{new_key}"
                    
                    target_cursor.execute("""
                        INSERT INTO files (id, "key", storage_type, provider, bucket, url, name, category, user_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (file_id, new_key, "local", "", "", file_url, original_filename, file_category, creator_id, ts_seconds))

                    echo_file_id = generate_uuid7(ts_ms + sort_order + 100)
                    target_cursor.execute("""
                        INSERT INTO echo_files (id, echo_id, file_id, sort_order)
                        VALUES (?, ?, ?, ?)
                    """, (echo_file_id, echo_id, file_id, sort_order))
            
            target_conn.commit()
            success_count += 1

        except Exception as e:
            target_conn.rollback()
            print(f"    [-] 迁移 Memo ID {memo_id} 时出错: {e}")
            fail_count += 1
            continue
    source_conn.close()
    target_conn.close()

    print(f"\n[+] 迁移完成！成功: {success_count} 条，失败: {fail_count} 条。")

if __name__ == "__main__":
    main()
