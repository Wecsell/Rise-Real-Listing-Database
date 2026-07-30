import asyncio
import os
from dotenv import load_dotenv

try:
    import asyncpg
except ImportError:
    asyncpg = None

load_dotenv(override=True)
DATABASE_URL = os.environ.get('DATABASE_URL')

async def reset_sequences():
    if not asyncpg:
        print("asyncpg не установлен. Пропуск сброса PostgreSQL.")
        return
    if not DATABASE_URL:
        print("DATABASE_URL не найден в .env.")
        return
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        print("Подключено к PostgreSQL.")
        
        # Сброс последовательностей ID
        sequences = ["extractions_id_seq", "facts_id_seq", "gpx_tracks_id_seq", "track_points_id_seq"]
        for seq in sequences:
            try:
                await conn.execute(f"ALTER SEQUENCE IF EXISTS {seq} RESTART WITH 1;")
                print(f"Последовательность {seq} сброшена на 1.")
            except Exception as e:
                print(f"Не удалось сбросить {seq}: {e}")
                
        await conn.close()
        print("Сброс счетчиков ID успешно завершен!")
    except Exception as e:
        print(f"Ошибка при подключении к БД: {e}")

if __name__ == "__main__":
    asyncio.run(reset_sequences())
