"""
Асинхронный backend для загрузчика видео
Поддерживает YouTube, ВКонтакте, Instagram
С псевдоочередью для разгрузки бесплатного хостинга
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, List
import asyncio
import uuid
import os
import shutil
from datetime import datetime, timedelta
import yt_dlp
from pathlib import Path

app = FastAPI()

# CORS middleware для работы с фронтендом
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене замените на конкретные домены
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Конфигурация
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
MAX_QUEUE_SIZE = 5  # Максимальный размер очереди
CLEANUP_AFTER_HOURS = 2  # Очистка файлов через N часов
MAX_QUALITY = 720  # Максимальное реальное качество (даже если запросили 1080p)

# Хранилище задач и очереди
tasks: Dict[str, dict] = {}
queue: List[str] = []
processing_task_id: Optional[str] = None


class DownloadRequest(BaseModel):
    url: str
    quality: int = 720


class TaskStatus(BaseModel):
    task_id: str
    status: str
    queue_position: Optional[int] = None
    progress: Optional[int] = None
    error: Optional[str] = None
    video_info: Optional[dict] = None


def detect_platform(url: str) -> str:
    """Определяет платформу по URL"""
    url_lower = url.lower()
    
    if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    elif 'vk.com' in url_lower or 'vkvideo.ru' in url_lower:
        return 'vk'
    elif 'instagram.com' in url_lower:
        return 'instagram'
    else:
        return 'unknown'


def get_ydl_opts(quality: int, output_path: str, platform: str) -> dict:
    """
    Возвращает опции для yt-dlp с учетом платформы и обходом защиты
    """
    # Ограничиваем качество максимумом
    actual_quality = min(quality, MAX_QUALITY)
    
    # Базовые опции
    base_opts = {
        'format': f'bestvideo[height<={actual_quality}]+bestaudio/best[height<={actual_quality}]/best',
        'outtmpl': output_path,
        'quiet': False,
        'no_warnings': False,
        'extract_flat': False,
        'nocheckcertificate': True,
    }
    
    # Специфичные опции для YouTube (обход защиты от роботов)
    if platform == 'youtube':
        youtube_opts = {
            # Используем cookies и симулируем браузер
            'cookiefile': None,  # Можно добавить файл с cookies если нужно
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'referer': 'https://www.youtube.com/',
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'web'],
                    'player_skip': ['webpage', 'configs'],
                }
            },
            # Добавляем задержки для имитации человека
            'sleep_interval': 1,
            'max_sleep_interval': 3,
            'sleep_interval_requests': 1,
            # Обходим age gate
            'age_limit': None,
        }
        base_opts.update(youtube_opts)
    
    # Специфичные опции для ВКонтакте
    elif platform == 'vk':
        vk_opts = {
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'http_headers': {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            }
        }
        base_opts.update(vk_opts)
    
    # Специфичные опции для Instagram
    elif platform == 'instagram':
        instagram_opts = {
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'http_headers': {
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
            }
        }
        base_opts.update(instagram_opts)
    
    return base_opts


async def download_video(task_id: str, url: str, quality: int):
    """Асинхронная загрузка видео"""
    global processing_task_id
    
    try:
        # Обновляем статус на "processing"
        tasks[task_id]['status'] = 'processing'
        tasks[task_id]['progress'] = 10
        processing_task_id = task_id
        
        # Определяем платформу
        platform = detect_platform(url)
        if platform == 'unknown':
            raise Exception("Неподдерживаемая платформа. Поддерживаются: YouTube, VK, Instagram")
        
        tasks[task_id]['platform'] = platform
        tasks[task_id]['progress'] = 20
        
        # Создаем уникальную директорию для задачи
        task_dir = DOWNLOAD_DIR / task_id
        task_dir.mkdir(exist_ok=True)
        
        output_template = str(task_dir / '%(title)s.%(ext)s')
        
        # Настраиваем yt-dlp
        ydl_opts = get_ydl_opts(quality, output_template, platform)
        
        tasks[task_id]['progress'] = 30
        
        # Извлекаем информацию о видео (асинхронно)
        loop = asyncio.get_event_loop()
        
        def extract_info():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)
        
        # Запускаем в executor для асинхронности
        info = await loop.run_in_executor(None, extract_info)
        
        tasks[task_id]['progress'] = 90
        
        # Сохраняем информацию о видео
        video_info = {
            'title': info.get('title', 'Видео'),
            'duration': format_duration(info.get('duration', 0)),
            'quality': f"{min(quality, MAX_QUALITY)}p",
            'size': 'Уточняется',
        }
        
        # Находим скачанный файл
        files = list(task_dir.glob('*'))
        if files:
            video_file = files[0]
            file_size = video_file.stat().st_size
            video_info['size'] = format_size(file_size)
            tasks[task_id]['file_path'] = str(video_file)
        
        tasks[task_id]['video_info'] = video_info
        tasks[task_id]['status'] = 'completed'
        tasks[task_id]['progress'] = 100
        tasks[task_id]['completed_at'] = datetime.now()
        
    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(e)
        print(f"Error downloading video for task {task_id}: {e}")
    
    finally:
        processing_task_id = None
        # Запускаем следующую задачу из очереди
        await process_next_in_queue()


async def process_next_in_queue():
    """Обрабатывает следующую задачу из очереди"""
    global processing_task_id, queue
    
    if processing_task_id is None and queue:
        next_task_id = queue.pop(0)
        task = tasks.get(next_task_id)
        
        if task and task['status'] == 'queued':
            # Запускаем обработку в фоне
            asyncio.create_task(
                download_video(
                    next_task_id,
                    task['url'],
                    task['quality']
                )
            )


def format_duration(seconds: int) -> str:
    """Форматирует длительность в читаемый вид"""
    if not seconds:
        return "Неизвестно"
    
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"


def format_size(bytes_size: int) -> str:
    """Форматирует размер файла"""
    for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} ТБ"


async def cleanup_old_files():
    """Периодическая очистка старых файлов"""
    while True:
        try:
            current_time = datetime.now()
            tasks_to_remove = []
            
            for task_id, task in tasks.items():
                if task.get('completed_at'):
                    age = current_time - task['completed_at']
                    if age > timedelta(hours=CLEANUP_AFTER_HOURS):
                        # Удаляем файлы
                        task_dir = DOWNLOAD_DIR / task_id
                        if task_dir.exists():
                            shutil.rmtree(task_dir)
                        tasks_to_remove.append(task_id)
            
            # Удаляем задачи из памяти
            for task_id in tasks_to_remove:
                del tasks[task_id]
            
            if tasks_to_remove:
                print(f"Cleaned up {len(tasks_to_remove)} old tasks")
        
        except Exception as e:
            print(f"Error in cleanup: {e}")
        
        # Запускаем каждый час
        await asyncio.sleep(3600)


@app.on_event("startup")
async def startup_event():
    """Запуск фоновых задач при старте приложения"""
    asyncio.create_task(cleanup_old_files())


@app.get("/")
async def root():
    """Корневой эндпоинт"""
    return {
        "message": "Video Downloader API",
        "version": "1.0.0",
        "endpoints": {
            "download": "/api/download",
            "status": "/api/status/{task_id}",
            "download_file": "/api/download/{task_id}"
        }
    }


@app.post("/api/download")
async def create_download_task(request: DownloadRequest):
    """
    Создает задачу на загрузку видео
    """
    # Проверяем размер очереди
    if len(queue) >= MAX_QUEUE_SIZE:
        raise HTTPException(
            status_code=429,
            detail="Очередь заполнена. Пожалуйста, попробуйте позже."
        )
    
    # Генерируем уникальный ID задачи
    task_id = str(uuid.uuid4())
    
    # Создаем задачу
    task = {
        'task_id': task_id,
        'url': request.url,
        'quality': request.quality,
        'status': 'queued',
        'created_at': datetime.now(),
        'progress': 0
    }
    
    tasks[task_id] = task
    
    # Добавляем в очередь
    queue.append(task_id)
    queue_position = len(queue)
    
    # Если нет активной обработки, запускаем
    if processing_task_id is None:
        await process_next_in_queue()
    
    return {
        'task_id': task_id,
        'status': 'queued',
        'queue_position': queue_position,
        'message': 'Задача добавлена в очередь'
    }


@app.get("/api/status/{task_id}")
async def get_task_status(task_id: str):
    """
    Получает статус задачи
    """
    task = tasks.get(task_id)
    
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    response = {
        'task_id': task_id,
        'status': task['status'],
        'progress': task.get('progress', 0)
    }
    
    # Добавляем позицию в очереди, если задача в очереди
    if task['status'] == 'queued' and task_id in queue:
        response['queue_position'] = queue.index(task_id) + 1
    
    # Добавляем информацию о видео, если загрузка завершена
    if task['status'] == 'completed':
        response['video_info'] = task.get('video_info', {})
    
    # Добавляем ошибку, если есть
    if task['status'] == 'error':
        response['error'] = task.get('error', 'Неизвестная ошибка')
    
    return response


@app.get("/api/download/{task_id}")
async def download_file(task_id: str):
    """
    Скачивает готовый файл
    """
    task = tasks.get(task_id)
    
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    if task['status'] != 'completed':
        raise HTTPException(
            status_code=400,
            detail=f"Видео еще не готово. Статус: {task['status']}"
        )
    
    file_path = task.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    # Возвращаем файл
    filename = os.path.basename(file_path)
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type='application/octet-stream'
    )


@app.get("/api/queue")
async def get_queue_status():
    """
    Получает информацию о текущей очереди (для отладки)
    """
    return {
        'queue_size': len(queue),
        'max_queue_size': MAX_QUEUE_SIZE,
        'processing_task': processing_task_id,
        'total_tasks': len(tasks),
        'queue': queue
    }


@app.delete("/api/task/{task_id}")
async def cancel_task(task_id: str):
    """
    Отменяет задачу
    """
    task = tasks.get(task_id)
    
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    # Удаляем из очереди если там есть
    if task_id in queue:
        queue.remove(task_id)
    
    # Удаляем файлы если есть
    task_dir = DOWNLOAD_DIR / task_id
    if task_dir.exists():
        shutil.rmtree(task_dir)
    
    # Удаляем задачу
    del tasks[task_id]
    
    return {'message': 'Задача отменена'}


if __name__ == "__main__":
    import uvicorn
    
    # Запуск сервера
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )

