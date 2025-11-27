import os
import sqlite3
import datetime
import json
import time
import threading
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from PIL import Image, ImageOps
from io import BytesIO
from typing import List, Optional, Dict
import hashlib
# --- 系统文件对话框 ---
import tkinter as tk
from tkinter import filedialog

# --- 配置 ---
DB_FILE = "metadata.db"
CACHE_FILE = "scan_cache.json"
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ReorderRequest(BaseModel):
    photo_paths: List[str]

class ReorderRootsRequest(BaseModel):
    root_paths: List[str]

class PhotoUpdate(BaseModel):
    path: str
    rating: Optional[int] = None
    color: Optional[str] = None
    tags: Optional[List[str]] = None
    description: Optional[str] = None


class CoverUpdate(BaseModel):
    folder: str
    photo_path: str


# --- 数据库处理 ---

def get_db_connection():
    # timeout=30: 如果数据库被锁，等待30秒而不是立刻报错
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    # ★ 开启 WAL 模式 (Write-Ahead Logging)
    # 这允许多个读取者和一个写入者同时操作，大幅解决 database is locked 问题
    c.execute("PRAGMA journal_mode=WAL")

    # 基础表结构
    c.execute('''CREATE TABLE IF NOT EXISTS photos 
                 (path TEXT PRIMARY KEY, rating INTEGER DEFAULT 0, 
                  color TEXT, tags TEXT, description TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS config 
                 (key TEXT PRIMARY KEY, value TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS album_covers 
                 (folder_path TEXT PRIMARY KEY, photo_path TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS album_ranks 
                     (folder_path TEXT PRIMARY KEY, rank INTEGER)''')
    # 动态添加字段 (兼容旧数据库)
    columns = [
        ("width", "INTEGER"),
        ("height", "INTEGER"),
        ("is_live", "INTEGER DEFAULT 0"),
        ("rank", "INTEGER DEFAULT 0"),
        ("last_modified", "REAL DEFAULT 0")  # ★ 新增：记录文件最后修改时间戳
    ]

    for col_name, col_type in columns:
        try:
            c.execute(f"ALTER TABLE photos ADD COLUMN {col_name} {col_type}")
        except:
            pass

    conn.commit()
    conn.close()


init_db()


# --- 辅助函数 ---

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
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img


def check_is_live(path):
    try:
        if not path.lower().endswith(('.jpg', '.jpeg')): return 0
        if os.path.getsize(path) < 1024 * 1024: return 0
        with open(path, 'rb') as f:
            data = f.read()
            idx = data.rfind(b'ftyp')
            if idx > 1000: return 1
    except:
        pass
    return 0


# --- 核心逻辑优化 ---

def perform_scan_logic():
    print(f"[{datetime.datetime.now()}] 开始极速扫描...")
    start_time = time.time()
    raw_roots = get_root_dirs()
    root_dirs = get_root_dirs()
    unique_roots = []
    sorted_roots = sorted(list(set(raw_roots)), key=len)
    for r in sorted_roots:
        r = os.path.normpath(r)
        # 检查 r 是否是 unique_roots 中某个路径的子目录
        is_subdir = False
        for parent in unique_roots:
            # 简单的字符串前缀检查，加 os.sep 确保是目录匹配
            if r.startswith(parent + os.sep) or r == parent:
                is_subdir = True
                break
        if not is_subdir:
            unique_roots.append(r)

    # 如果清理后发现 roots 变少了，说明之前有污染，自动修正配置
    if len(unique_roots) < len(raw_roots):
        print(f"发现重复嵌套的根目录，已自动清理: {len(raw_roots)} -> {len(unique_roots)}")
        set_root_dirs(unique_roots)

    conn = get_db_connection()

    # 1. 预加载所有数据库记录到内存 (HashMap)
    # 这样后续查询就是 O(1) 的内存操作，不需要反复查询数据库
    # key: path, value: row
    print("正在加载数据库缓存...")
    db_cache = {}
    try:
        cursor = conn.execute("SELECT * FROM photos")
        rows = cursor.fetchall()
        for row in rows:
            # 将 sqlite3.Row 转为普通 dict 方便后续使用
            db_cache[row['path']] = dict(row)
    except Exception as e:
        print(f"DB Load Error: {e}")
    ranks_map = {}
    try:
        rows = conn.execute("SELECT folder_path, rank FROM album_ranks").fetchall()
        for r in rows:
            ranks_map[r['folder_path']] = r['rank']
    except:
        pass
    # 获取封面 map
    covers_map = {}
    try:
        rows = conn.execute("SELECT folder_path, photo_path FROM album_covers").fetchall()
        for r in rows:
            covers_map[r['folder_path']] = r['photo_path']
    except:
        pass

    all_galleries = []

    # 待更新/插入的列表 (用于批量写入)
    to_upsert = []

    scanned_count = 0
    skipped_count = 0

    for ROOT_DIR in root_dirs:
        if not os.path.exists(ROOT_DIR): continue
        ROOT_DIR = os.path.normpath(ROOT_DIR)

        for root, dirs, files in os.walk(ROOT_DIR):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            image_files = [f for f in files if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'))]

            if not image_files: continue

            album_photos = []

            for f in image_files:
                full_path = os.path.normpath(os.path.join(root, f))

                # 获取当前文件系统的时间戳
                try:
                    current_mtime = os.path.getmtime(full_path)
                except:
                    continue  # 文件可能被删了

                # --- ⚡⚡⚡ 极速优化核心 ⚡⚡⚡ ---
                # 检查缓存里有没有这个文件，且时间戳是否一致
                cached_data = db_cache.get(full_path)

                need_process = True

                # 如果缓存存在，且文件修改时间(误差0.1秒内)一致，直接使用缓存
                if cached_data:
                    last_mod = cached_data.get('last_modified', 0)
                    if last_mod and abs(last_mod - current_mtime) < 1.0:
                        # 命中缓存！直接使用数据库数据，完全跳过 Image.open
                        photo_data = cached_data
                        need_process = False
                        skipped_count += 1
                    else:
                        # 文件被修改过，需要重新读取
                        photo_data = cached_data
                else:
                    # 新文件
                    photo_data = {}

                # 只有当需要处理时（新文件 or 被修改）才执行耗时操作
                if need_process:
                    scanned_count += 1
                    width = 0
                    height = 0
                    is_live = 0

                    # 尝试读取图片信息
                    try:
                        # 只有在新文件或需要更新时才检测 Live
                        is_live = check_is_live(full_path)

                        with Image.open(full_path) as img:
                            width, height = img.size
                    except:
                        width, height = 100, 100

                    # 准备存入数据库的数据
                    # 保留原有的用户数据（评分、标签等），只更新文件属性
                    new_record = {
                        "path": full_path,
                        "rating": photo_data.get('rating', 0),
                        "color": photo_data.get('color', 'none'),
                        "tags": photo_data.get('tags', ''),
                        "description": photo_data.get('description', ''),
                        "width": width,
                        "height": height,
                        "is_live": is_live,
                        "rank": photo_data.get('rank', 0),
                        "last_modified": current_mtime

                    }

                    to_upsert.append(new_record)

                    # 更新内存里的对象，用于生成前端 JSON
                    photo_data = new_record

                # 构造前端需要的数据结构
                tags_list = []
                if photo_data.get('tags'):
                    tags_list = photo_data['tags'].split(",")

                album_photos.append({
                    "name": f,
                    "path": full_path,
                    "date": datetime.datetime.fromtimestamp(current_mtime).strftime('%Y-%m-%d'),
                    "width": photo_data.get('width', 0),
                    "height": photo_data.get('height', 0),
                    "rating": photo_data.get('rating', 0),
                    "color": photo_data.get('color', 'none'),
                    "tags": tags_list,
                    "description": photo_data.get('description', ''),
                    "rank": photo_data.get('rank', 0),
                    "is_live": photo_data.get('is_live', 0)


                })

            # 封面逻辑
            current_folder_path = os.path.normpath(root)
            cover_image = covers_map.get(current_folder_path)
            if not cover_image and album_photos:
                cover_image = album_photos[0]['path']

            rel_path = os.path.relpath(root, ROOT_DIR)
            display_name = os.path.basename(ROOT_DIR) if rel_path == "." else os.path.basename(root)

            all_galleries.append({
                "folder_path": current_folder_path,
                "name": display_name,
                "root_root": ROOT_DIR,
                "rel_path": rel_path,
                "cover": cover_image,
                "photos": album_photos,
                "count": len(album_photos),
                "rank": ranks_map.get(current_folder_path, 999999)
            })

    # 3. 批量写入数据库 (Batch Insert/Update)
    if to_upsert:
        print(f"正在批量更新数据库 ({len(to_upsert)} 条记录)...")
        try:
            # 使用 INSERT OR REPLACE
            sql = '''INSERT OR REPLACE INTO photos 
                     (path, rating, color, tags, description, width, height, is_live, rank, last_modified) 
                     VALUES (:path, :rating, :color, :tags, :description, :width, :height, :is_live, :rank, :last_modified)'''
            conn.executemany(sql, to_upsert)
            conn.commit()
        except Exception as e:
            print(f"Batch Update Error: {e}")

    all_galleries.sort(key=lambda x: (x['rank'], x['name']))

    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(all_galleries, f, ensure_ascii=False)
    except Exception as e:
        print(f"Cache write error: {e}")

    conn.close()

    end_time = time.time()
    print(f"扫描完成。耗时: {end_time - start_time:.2f}秒. 跳过: {skipped_count}, 处理: {scanned_count}")

    return all_galleries


def background_scan_task():
    perform_scan_logic()


# --- API ---

@app.get("/api/pick_folder")
def pick_folder():
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
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


# 新增/替换 API
@app.post("/api/reorder_albums")  # 改个名字区分一下
def reorder_albums_api(data: ReorderRootsRequest):
    try:
        conn = get_db_connection()
        # 批量更新 rank
        # data.root_paths 是前端排好序的列表，index 就是 rank
        params = [(i, path) for i, path in enumerate(data.root_paths)]

        # 使用 REPLACE INTO 确保存在
        conn.executemany("INSERT OR REPLACE INTO album_ranks (rank, folder_path) VALUES (?, ?)", params)

        conn.commit()
        conn.close()

        # 删除缓存，强制刷新
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)

        return {"status": "success"}
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/roots")
def delete_root(path: str):
    roots = get_root_dirs()
    norm_path = os.path.normpath(path)
    target_to_remove = None
    for r in roots:
        if os.path.normpath(r).lower() == norm_path.lower():
            target_to_remove = r
            break
    if target_to_remove:
        roots.remove(target_to_remove)
        set_root_dirs(roots)
        if os.path.exists(CACHE_FILE):
            try:
                os.remove(CACHE_FILE)
            except:
                pass
        return {"status": "success", "roots": roots}
    return {"status": "error", "message": "Root not found"}


@app.get("/api/scan")
def scan_photos(background_tasks: BackgroundTasks, force: bool = False):
    if force or not os.path.exists(CACHE_FILE):
        return perform_scan_logic()
    background_tasks.add_task(background_scan_task)
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data
    except:
        return perform_scan_logic()


@app.post("/api/reorder")
def reorder_photos(data: ReorderRequest):
    try:
        conn = get_db_connection()
        params = [(i, path) for i, path in enumerate(data.photo_paths)]
        conn.executemany("UPDATE photos SET rank=? WHERE path=?", params)
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/reorder_roots")
def reorder_roots(data: ReorderRootsRequest):
    """更新根目录的顺序"""
    try:
        # data.root_paths 是前端传来的排好序的路径列表
        # 我们直接覆盖 config 表里的 roots
        set_root_dirs(data.root_paths)

        # 顺便清空缓存，强制下次刷新
        if os.path.exists(CACHE_FILE):
            try:
                os.remove(CACHE_FILE)
            except:
                pass

        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/live_video")
def get_live_video(path: str):
    if not os.path.exists(path): return FileResponse(path)
    try:
        with open(path, 'rb') as f:
            data = f.read()
        idx = data.rfind(b'ftyp')
        if idx > 4:
            video_data = data[idx - 4:]
            headers = {"Cache-Control": "public, max-age=31536000"}
            return StreamingResponse(BytesIO(video_data), media_type="video/mp4", headers=headers)
        raise HTTPException(status_code=404, detail="No video found")
    except:
        raise HTTPException(status_code=500, detail="Error")


@app.get("/api/download")
def download_image(path: str):
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    filename = os.path.basename(path)
    return FileResponse(path, filename=filename)


# 缩略图缓存
THUMB_CACHE_DIR = ".thumb_cache"
if not os.path.exists(THUMB_CACHE_DIR): os.makedirs(THUMB_CACHE_DIR)


@app.get("/api/image")
def get_image(path: str, thumb: bool = False):
    if not os.path.exists(path): return FileResponse(path)
    headers = {"Cache-Control": "public, max-age=31536000"}
    if thumb:
        path_hash = hashlib.md5(path.encode('utf-8')).hexdigest()
        thumb_path = os.path.join(THUMB_CACHE_DIR, f"{path_hash}.jpg")
        if os.path.exists(thumb_path):
            return FileResponse(thumb_path, headers=headers)
        try:
            with Image.open(path) as img:
                img = fix_orientation(img)
                if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                img.thumbnail((400, 400))
                img.save(thumb_path, format="JPEG", quality=80)
                return FileResponse(thumb_path, headers=headers)
        except:
            pass
    return FileResponse(path, headers=headers)


@app.post("/api/update")
def update_metadata(data: PhotoUpdate):
    try:
        conn = get_db_connection()
        # 先尝试插入(如果不存在)，如果存在则忽略
        conn.execute("INSERT OR IGNORE INTO photos (path) VALUES (?)", (data.path,))

        updates = []
        params = []
        if data.rating is not None:
            updates.append("rating=?")
            params.append(data.rating)
        if data.color is not None:
            updates.append("color=?")
            params.append(data.color)
        if data.description is not None:
            updates.append("description=?")
            params.append(data.description)
        if data.tags is not None:
            updates.append("tags=?")
            params.append(",".join(data.tags))

        if updates:
            sql = f"UPDATE photos SET {', '.join(updates)} WHERE path=?"
            params.append(data.path)
            conn.execute(sql, params)
            conn.commit()

        conn.close()
        return {"status": "success"}
    except Exception as e:
        print(f"Update error: {e}")
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
    except:
        raise HTTPException(status_code=500, detail="Error")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    if os.path.exists(full_path) and os.path.isfile(full_path):
        return FileResponse(full_path)
    return FileResponse('index.html')


if __name__ == "__main__":
    import uvicorn

    if not os.path.exists(DB_FILE):
        init_db()
    print("启动中... 请访问 http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)