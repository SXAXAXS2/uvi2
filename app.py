from quart import Quart, request, jsonify
from quart_cors import cors
import yt_dlp
import os
import asyncio
import uuid
from datetime import datetime, timedelta
from pathlib import Path
import shutil

app = Quart(__name__)
app = cors(app, allow_origin="*")

# Конфигурация
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
MAX_QUEUE_SIZE = 5
MAX_QUALITY = 720
CLEANUP_AFTER_HOURS = 2

# Хранилище задач и очередь
tasks = {}
queue = []
processing_task_id = None

# Настройки yt-dlp для обхода проверки бота
YDL_OPTS_BASE = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    'nocheckcertificate': True,
    'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'referer': 'https://www.youtube.com/',
    'sleep_interval': 1,
    'max_sleep_interval': 3,
}

def detect_platform(url):
    """Определяет платформу по URL"""
    url_lower = url.lower()
    if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    elif 'vk.com' in url_lower or 'vkvideo.ru' in url_lower:
        return 'vk'
    elif 'instagram.com' in url_lower:
        return 'instagram'
    return 'unknown'

def format_duration(seconds):
    """Форматирует длительность в читаемый вид"""
    if not seconds:
        return "Неизвестно"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def format_size(bytes_size):
    """Форматирует размер файла"""
    for unit in ['Б', 'КБ', 'МБ', 'ГБ']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} ТБ"

async def download_video(task_id, url, quality):
    """Асинхронная загрузка видео"""
    global processing_task_id
    
    try:
        tasks[task_id]['status'] = 'processing'
        tasks[task_id]['progress'] = 10
        processing_task_id = task_id
        
        platform = detect_platform(url)
        if platform == 'unknown':
            raise Exception("Неподдерживаемая платформа")
        
        tasks[task_id]['progress'] = 20
        
        # Ограничиваем качество
        actual_quality = min(quality, MAX_QUALITY)
        
        task_dir = DOWNLOAD_DIR / task_id
        task_dir.mkdir(exist_ok=True)
        
        output_template = str(task_dir / '%(title)s.%(ext)s')
        
        ydl_opts = {
            **YDL_OPTS_BASE,
            'format': f'bestvideo[height<={actual_quality}]+bestaudio/best[height<={actual_quality}]/best',
            'outtmpl': output_template,
        }
        
        tasks[task_id]['progress'] = 30
        
        # Запускаем загрузку в executor
        loop = asyncio.get_event_loop()
        
        def sync_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)
        
        info = await loop.run_in_executor(None, sync_download)
        
        tasks[task_id]['progress'] = 90
        
        # Сохраняем информацию о видео
        video_info = {
            'title': info.get('title', 'Видео'),
            'duration': format_duration(info.get('duration', 0)),
            'quality': f"{actual_quality}p",
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
    
    finally:
        processing_task_id = None
        await process_next_in_queue()

async def process_next_in_queue():
    """Обрабатывает следующую задачу из очереди"""
    global processing_task_id, queue
    
    if processing_task_id is None and queue:
        next_task_id = queue.pop(0)
        task = tasks.get(next_task_id)
        
        if task and task['status'] == 'queued':
            asyncio.create_task(
                download_video(
                    next_task_id,
                    task['url'],
                    task['quality']
                )
            )

async def cleanup_old_files():
    """Периодическая очистка старых файлов"""
    while True:
        try:
            current_time = datetime.now()
            tasks_to_remove = []
            
            for task_id, task in list(tasks.items()):
                if task.get('completed_at'):
                    age = current_time - task['completed_at']
                    if age > timedelta(hours=CLEANUP_AFTER_HOURS):
                        task_dir = DOWNLOAD_DIR / task_id
                        if task_dir.exists():
                            shutil.rmtree(task_dir)
                        tasks_to_remove.append(task_id)
            
            for task_id in tasks_to_remove:
                del tasks[task_id]
        
        except Exception as e:
            print(f"Cleanup error: {e}")
        
        await asyncio.sleep(3600)

@app.before_serving
async def startup():
    """Запуск фоновых задач при старте приложения"""
    asyncio.create_task(cleanup_old_files())

@app.route('/')
async def home():
    return jsonify({
        'status': 'ok',
        'message': 'Video Downloader API',
        'version': '2.0',
        'endpoints': {
            '/api/download': 'POST - Create download task',
            '/api/status/<task_id>': 'GET - Get task status',
            '/api/download/<task_id>': 'GET - Download file'
        }
    })

@app.route('/api/download', methods=['POST'])
async def create_download_task():
    """Создает задачу на загрузку видео"""
    try:
        data = await request.get_json()
        url = data.get('url')
        quality = int(data.get('quality', 720))
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        
        # Проверяем размер очереди
        if len(queue) >= MAX_QUEUE_SIZE:
            return jsonify({'error': 'Очередь заполнена. Попробуйте позже.'}), 429
        
        # Генерируем уникальный ID задачи
        task_id = str(uuid.uuid4())
        
        # Создаем задачу
        task = {
            'task_id': task_id,
            'url': url,
            'quality': quality,
            'status': 'queued',
            'created_at': datetime.now(),
            'progress': 0
        }
        
        tasks[task_id] = task
        queue.append(task_id)
        queue_position = len(queue)
        
        # Если нет активной обработки, запускаем
        if processing_task_id is None:
            await process_next_in_queue()
            
        return jsonify({
            'task_id': task_id,
            'status': 'queued',
            'queue_position': queue_position,
            'message': 'Задача добавлена в очередь'
        })
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/status/<task_id>', methods=['GET'])
async def get_task_status(task_id):
    """Получает статус задачи"""
    task = tasks.get(task_id)
    
    if not task:
        return jsonify({'error': 'Задача не найдена'}), 404
    
    response = {
        'task_id': task_id,
        'status': task['status'],
        'progress': task.get('progress', 0)
    }
    
    # Добавляем позицию в очереди
    if task['status'] == 'queued' and task_id in queue:
        response['queue_position'] = queue.index(task_id) + 1
    
    # Добавляем информацию о видео
    if task['status'] == 'completed':
        response['video_info'] = task.get('video_info', {})
    
    # Добавляем ошибку
    if task['status'] == 'error':
        response['error'] = task.get('error', 'Неизвестная ошибка')
    
    return jsonify(response)

@app.route('/api/download/<task_id>', methods=['GET'])
async def download_file(task_id):
    """Скачивает готовый файл"""
    task = tasks.get(task_id)
    
    if not task:
        return jsonify({'error': 'Задача не найдена'}), 404
    
    if task['status'] != 'completed':
        return jsonify({'error': f'Видео еще не готово. Статус: {task["status"]}'}), 400
    
    file_path = task.get('file_path')
    if not file_path or not os.path.exists(file_path):
        return jsonify({'error': 'Файл не найден'}), 404
    
    return await app.send_file(file_path, as_attachment=True)

@app.route('/api/queue', methods=['GET'])
async def get_queue_status():
    """Получает информацию о текущей очереди"""
    return jsonify({
        'queue_size': len(queue),
        'max_queue_size': MAX_QUEUE_SIZE,
        'processing_task': processing_task_id,
        'total_tasks': len(tasks),
        'queue': queue
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
