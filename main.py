import os
import sqlite3
import datetime
import json
import threading
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from PIL import Image, ImageOps
from io import BytesIO
from typing import List, Optional
import hashlib
# --- 系统文件对话框 ---
import tkinter as tk
from tkinter import filedialog

# --- 配置 ---
DB_FILE = "metadata.db"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 1. 照片元数据表 (增加 description)
    # 如果表已存在但没有 description 列，需要手动处理(这里为了简化，建议删除旧db重新生成，或者手动alter)
    try:
        c.execute("ALTER TABLE photos ADD COLUMN description TEXT")
    except:
        pass  # 列已存在或表不存在

    c.execute('''CREATE TABLE IF NOT EXISTS photos 
                 (path TEXT PRIMARY KEY, rating INTEGER DEFAULT 0, 
                  color TEXT, tags TEXT, description TEXT)''')

    # 2. 配置表
    c.execute('''CREATE TABLE IF NOT EXISTS config 
                 (key TEXT PRIMARY KEY, value TEXT)''')

    # 3. 相册封面表 (New)
    c.execute('''CREATE TABLE IF NOT EXISTS album_covers 
                 (folder_path TEXT PRIMARY KEY, photo_path TEXT)''')

    # 1. 修改 photos 表结构，增加 width 和 height
    # 注意：如果你的 metadata.db 已经存在，建议手动删除该文件让程序重新生成，或者运行下面的补充语句：
    try:
        c.execute("ALTER TABLE photos ADD COLUMN width INTEGER")
        c.execute("ALTER TABLE photos ADD COLUMN height INTEGER")
    except:
        pass

        # 确保建表语句包含这两个新字段
    c.execute('''CREATE TABLE IF NOT EXISTS photos 
                     (path TEXT PRIMARY KEY, rating INTEGER DEFAULT 0, 
                      color TEXT, tags TEXT, description TEXT, 
                      width INTEGER, height INTEGER)''')
    try:
        c.execute("ALTER TABLE photos ADD COLUMN is_live INTEGER DEFAULT 0")
    except:
        pass

        # 确保建表语句也包含
    c.execute('''CREATE TABLE IF NOT EXISTS photos 
                     (path TEXT PRIMARY KEY, rating INTEGER DEFAULT 0, 
                      color TEXT, tags TEXT, description TEXT, 
                      width INTEGER, height INTEGER, is_live INTEGER)''')



    conn.commit()
    conn.close()


init_db()


# --- 辅助函数 ---

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_root_dirs():
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM config WHERE key='roots'").fetchone()
    conn.close()
    if row and row['value']:
        return json.loads(row['value'])
    return []


def set_root_dirs(roots: List[str]):
    conn = get_db_connection()
    roots_json = json.dumps(roots)
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ('roots', roots_json))
    conn.commit()
    conn.close()


def fix_orientation(img):
    """根据EXIF数据自动旋转图片"""
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img


# --- Pydantic Models ---

class PhotoUpdate(BaseModel):
    path: str
    rating: Optional[int] = None
    color: Optional[str] = None
    tags: Optional[List[str]] = None
    description: Optional[str] = None


class CoverUpdate(BaseModel):
    folder: str
    photo_path: str


# --- API ---

@app.get("/api/pick_folder")
def pick_folder():
    """在服务端打开文件夹选择框"""
    try:
        # Tkinter 必须在主线程运行，或者需要特殊处理。
        # 对于本地单人应用，这样简单的调用通常可行。
        root = tk.Tk()
        root.withdraw()  # 隐藏主窗口
        root.attributes('-topmost', True)  # 确保弹窗在最前
        folder_selected = filedialog.askdirectory()
        root.destroy()

        if folder_selected:
            folder_selected = os.path.normpath(folder_selected)
            current_roots = get_root_dirs()
            if folder_selected not in current_roots:
                current_roots.append(folder_selected)
                set_root_dirs(current_roots)
            return {"status": "success", "path": folder_selected}
        return {"status": "cancelled"}
    except Exception as e:
        print(e)
        return {"status": "error", "message": str(e)}


@app.get("/api/roots")
def list_roots():
    return get_root_dirs()


@app.delete("/api/roots")
def delete_root(path: str):
    roots = get_root_dirs()
    if path in roots:
        roots.remove(path)
        set_root_dirs(roots)
    return {"status": "success", "roots": roots}


def check_is_live(path):
    """检测是否为 Live Photo (通过检查文件末尾是否包含 MP4 结构)"""
    try:
        if not path.lower().endswith(('.jpg', '.jpeg')):
            return 0

        # 预判：文件太小肯定不是 Live
        file_size = os.path.getsize(path)
        if file_size < 1024 * 1024:  # 小于 1MB 直接跳过
            return 0

        with open(path, 'rb') as f:
            # 优化：为了性能，我们不需要读整个文件来检测
            # 只需要读最后 20MB (通常 Live 视频不会特别大) 或者读全部
            # 如果不想改太复杂，读全部也是最稳的
            data = f.read()

            # 逻辑必须和 get_live_video 保持一致
            idx = data.rfind(b'ftyp')

            # 如果找到了 ftyp，且位置不在文件开头（说明是拼接在后面的），就认为是 Live
            if idx > 1000:  # 至少给 JPG 留点空间
                return 1

    except Exception as e:
        print(f"Check live error: {e}")
        pass

    return 0
@app.get("/api/scan")
def scan_photos():
    root_dirs = get_root_dirs()
    conn = get_db_connection()

    # 获取所有自定义封面
    covers_map = {}
    rows = conn.execute("SELECT folder_path, photo_path FROM album_covers").fetchall()
    for r in rows:
        covers_map[r['folder_path']] = r['photo_path']

    all_galleries = []

    for ROOT_DIR in root_dirs:
        if not os.path.exists(ROOT_DIR):
            continue

        ROOT_DIR = os.path.normpath(ROOT_DIR)

        # 遍历目录
        for root, dirs, files in os.walk(ROOT_DIR):
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            image_files = [f for f in files if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'))]

            if not image_files:
                continue

            # 构建相册信息
            album_photos = []

            # 批量获取元数据
            # 这里的逻辑稍微简化，对于大量图片可能需要优化SQL查询
            for f in image_files:
                full_path = os.path.normpath(os.path.join(root, f))

                # 修改 SQL 查询，多查 width 和 height
                row = conn.execute(
                    "SELECT rating, color, tags, description, width, height, is_live FROM photos WHERE path=?",
                    (full_path,)).fetchone()

                tags_list = []
                if row and row['tags']:
                    tags_list = row['tags'].split(",")
                is_live = row['is_live'] if row else 0
                # 如果数据库里是 0，但我们没扫过（或者想强制重扫），可以重新检测
                # 这里逻辑：如果是新插入的(row不存在)，检测一下
                if not row or is_live == 0:
                    # 只有当确实检测到是 1 时，我们才更新。如果是 0，保持原状（避免重复检测耗时，虽然这里简单处理了）
                    # 为了更严谨，我们可以每次都检测，但为了性能，这里假设如果已经是 1 了就不用测了
                    if is_live == 0:
                        detected_live = check_is_live(full_path)
                        if detected_live:
                            is_live = 1
                            # 记得把新状态更新回数据库！
                            conn.execute("UPDATE photos SET is_live=1 WHERE path=?", (full_path,))
                            conn.commit()
                width = row['width'] if row else 0
                height = row['height'] if row else 0

                # 如果数据库里没有宽高数据，才去打开文件读取 (懒加载)
                if not width or not height:
                    try:
                        with Image.open(full_path) as img:
                            width, height = img.size
                            # 将读取到的宽高存回数据库，下次就不读文件了
                            conn.execute("UPDATE photos SET width=?, height=? WHERE path=?", (width, height, full_path))
                            conn.commit()  # 记得提交
                    except:
                        width, height = 100, 100

                album_photos.append({
                    "name": f,
                    "path": full_path,
                    "date": datetime.datetime.fromtimestamp(os.path.getmtime(full_path)).strftime('%Y-%m-%d'),
                    "width": width,
                    "height": height,
                    "rating": row['rating'] if row else 0,
                    "color": row['color'] if row else "none",
                    "tags": tags_list,
                    "description": row['description'] if row else "",
                    "is_live": is_live
                })

            # 确定封面
            current_folder_path = os.path.normpath(root)
            cover_image = None

            # 1. 用户自定义封面
            if current_folder_path in covers_map and os.path.exists(covers_map[current_folder_path]):
                cover_image = covers_map[current_folder_path]
            # 2. 默认第一张
            elif album_photos:
                cover_image = album_photos[0]['path']

            # 计算相对层级结构
            rel_path = os.path.relpath(root, ROOT_DIR)
            if rel_path == ".":
                display_name = os.path.basename(ROOT_DIR)
                parent = None
            else:
                display_name = os.path.basename(root)
                parent = os.path.dirname(rel_path)  # 逻辑上的父级标识，前端用来构建树

            all_galleries.append({
                "folder_path": current_folder_path,  # 唯一ID
                "name": display_name,
                "root_root": ROOT_DIR,  # 所属的根库
                "rel_path": rel_path,  # 相对路径
                "cover": cover_image,
                "photos": album_photos,
                "count": len(album_photos)
            })

    conn.close()
    return all_galleries


@app.get("/api/live_video")
def get_live_video(path: str):
    """从 Live Photo 中提取视频流"""
    if not os.path.exists(path):
        return FileResponse(path)

    try:
        with open(path, 'rb') as f:
            data = f.read()

        idx = data.rfind(b'ftyp')

        if idx > 4:
            video_start = idx - 4
            # ... (中间的校验逻辑保持不变) ...
            video_data = data[video_start:]

            # --- 修改重点：添加 Cache-Control 头 (缓存1年) ---
            headers = {"Cache-Control": "public, max-age=31536000"}

            return StreamingResponse(
                BytesIO(video_data),
                media_type="video/mp4",
                headers=headers  # <--- 加上这个
            )
        else:
            raise HTTPException(status_code=404, detail="No video found")

    except Exception as e:
        print(f"Video extract error: {e}")
        raise HTTPException(status_code=500, detail="Error extracting video")

# 定义缓存目录
THUMB_CACHE_DIR = ".thumb_cache"
if not os.path.exists(THUMB_CACHE_DIR):
    os.makedirs(THUMB_CACHE_DIR)


@app.get("/api/image")
def get_image(path: str, thumb: bool = False):
    if not os.path.exists(path):
        return FileResponse(path)  # 404 处理

    # 设置浏览器缓存头 (缓存 1 年)
    headers = {"Cache-Control": "public, max-age=31536000"}

    if thumb:
        # 生成唯一的缩略图文件名 (使用路径的 MD5)
        path_hash = hashlib.md5(path.encode('utf-8')).hexdigest()
        thumb_filename = f"{path_hash}.jpg"
        thumb_path = os.path.join(THUMB_CACHE_DIR, thumb_filename)

        # 1. 如果硬盘上已经有缩略图缓存，直接返回文件
        if os.path.exists(thumb_path):
            return FileResponse(thumb_path, headers=headers)

        # 2. 如果没有，生成并保存
        try:
            with Image.open(path) as img:
                img = fix_orientation(img)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                img.thumbnail((400, 400))

                # 保存到缓存文件夹
                img.save(thumb_path, format="JPEG", quality=80)

                # 返回生成的文件
                return FileResponse(thumb_path, headers=headers)
        except Exception as e:
            print(f"Thumb error: {e}")
            # 出错降级返回原图
            return FileResponse(path, headers=headers)

    # 返回原图时也加上缓存头
    return FileResponse(path, headers=headers)


@app.post("/api/update")
def update_metadata(data: PhotoUpdate):
    try:
        conn = get_db_connection()
        conn.execute("INSERT OR IGNORE INTO photos (path) VALUES (?)", (data.path,))

        if data.rating is not None:
            conn.execute("UPDATE photos SET rating=? WHERE path=?", (data.rating, data.path))
        if data.color is not None:
            conn.execute("UPDATE photos SET color=? WHERE path=?", (data.color, data.path))
        if data.description is not None:
            conn.execute("UPDATE photos SET description=? WHERE path=?", (data.description, data.path))
        if data.tags is not None:
            tags_str = ",".join(data.tags)
            conn.execute("UPDATE photos SET tags=? WHERE path=?", (tags_str, data.path))

        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/set_cover")
def set_album_cover(data: CoverUpdate):
    try:
        conn = get_db_connection()
        conn.execute("INSERT OR REPLACE INTO album_covers (folder_path, photo_path) VALUES (?, ?)",
                     (data.folder, data.photo_path))
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def read_index():
    return FileResponse('index.html')


if __name__ == "__main__":
    import uvicorn

    # 确保 metadata.db 存在
    if not os.path.exists(DB_FILE):
        init_db()
    print("启动中... 请访问 http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)