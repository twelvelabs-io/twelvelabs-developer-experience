import os
import sys
import array
import time
import oracledb
from twelvelabs import TwelveLabs
from twelvelabs.models.embed import EmbeddingsTask
import json
# from create_schema_video_embeddings import create_vector_index, drop_vector_index
import argparse

# Environment variables
db_username = os.getenv("ORACLE_DB_USERNAME")
db_password = os.getenv("ORACLE_DB_PASSWORD")
db_connect_string = os.getenv("ORACLE_DB_CONNECT_STRING")
db_wallet_path = os.getenv("ORACLE_DB_WALLET_PATH")
twelvelabs_api_key = os.getenv("TWELVE_LABS_API_KEY")

# Initialize Twelve Labs client
twelvelabs_client = TwelveLabs(api_key=twelvelabs_api_key)
if not twelvelabs_client.api_key:
    print('\nYou need to set your TWELVE_LABS_API_KEY\n')
    print('https://playground.twelvelabs.io/dashboard/api-key')
    print('export TWELVE_LABS_API_KEY=your_api_key_value\n')
    exit()

# Constants
EMBEDDING_MODEL = "Marengo-retrieval-2.7"
SEGMENT_DURATION = 6  # seconds per segment
TOP_K = 5  # number of results to return in similarity search


def on_task_update(task: EmbeddingsTask):
    print(f"  Status={task.status}")

def create_video_embeddings(client, video_file):
    """Create embeddings for a video file using Twelve Labs Marengo"""
    task = client.embed.task.create(
        model_name=EMBEDDING_MODEL,
        video_file=video_file,
        video_clip_length=SEGMENT_DURATION
    )
    print(f"Created task: id={task.id} model_name={EMBEDDING_MODEL} status={task.status}")

    status = task.wait_for_done(
        sleep_interval=2,
        callback=on_task_update
    )
    print(f"Embedding done: {status}")
    
    return task.id

def store_embeddings_in_db(connection, task_id, video_file):
    """Store video embeddings in Oracle DB"""
    task = twelvelabs_client.embed.task.retrieve(task_id)
    
    # Get embeddings from the task
    if not task.video_embedding or not task.video_embedding.segments:
        print("No embeddings found")
        return
    
    insert_sql = """
    INSERT INTO video_embeddings (
        id, video_file, start_time, end_time, embedding_vector
    ) VALUES (
        :1, :2, :3, :4, :5
    )"""
    
    BATCH_SIZE = 1000
    data_batch = []
    
    with connection.cursor() as cursor:
        for idx, segment in enumerate(task.video_embedding.segments):
            id = f"{task_id}_{idx}"
            vector = array.array("f", segment.embeddings_float)
            
            data_batch.append([
                id,
                video_file,
                segment.start_offset_sec,
                segment.end_offset_sec,
                vector
            ])
            
            # Execute and commit every BATCH_SIZE rows
            if len(data_batch) >= BATCH_SIZE:
                print("insert data")
                cursor.executemany(insert_sql, data_batch)
                connection.commit()
                data_batch = []
        
        # Insert any remaining rows
        if data_batch:
            print("insert data final")
            cursor.executemany(insert_sql, data_batch)
            connection.commit()

    print(f"Stored {len(task.video_embedding.segments)} embeddings in database")

def load_task_ids():
    """Load existing task IDs from JSON file"""
    try:
        with open('video_task_ids.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_task_ids(task_ids):
    """Save task IDs to JSON file"""
    with open('video_task_ids.json', 'w') as f:
        json.dump(task_ids, f, indent=2)

def process_video(connection, video_path):
    """Process a single video file"""
    print(f"\nProcessing video: {video_path}")
    
    # Load existing task IDs
    task_ids = load_task_ids()
    
    # If video was already processed, use existing task_id to store embeddings
    if video_path in task_ids:
        task_id = task_ids[video_path]
        print(f"Video previously processed with task_id: {task_id}")
        print("Re-storing embeddings in database...")
        try:
            store_embeddings_in_db(connection, task_id, video_path)
        except Exception as e:
            print(f"Error storing embeddings for {video_path}: {str(e)}")
        return
    
    try:
        # Create embeddings and store in DB
        print("Creating video embeddings...")
        task_id = create_video_embeddings(twelvelabs_client, video_path)
        
        # Store task_id in JSON
        task_ids[video_path] = task_id
        save_task_ids(task_ids)
        
        print("Storing embeddings in database...")
        store_embeddings_in_db(connection, task_id, video_path)
    except Exception as e:
        print(f"Error processing video {video_path}: {str(e)}")

def store_video_embeddings(video_path):
    """Process video file(s) and store embeddings in Oracle DB"""
    try:
        connection = oracledb.connect(
            user=db_username,
            password=db_password,
            dsn=db_connect_string,
            config_dir=db_wallet_path,
            wallet_location=db_wallet_path,
            wallet_password=db_password
        )
        connection.autocommit = True  # Enable autocommit
        
        # Verify DB version
        db_version = tuple(int(s) for s in connection.version.split("."))[:2]
        if db_version < (23, 7):
            sys.exit("This example requires Oracle Database 23.7 or later")
        print("Connected to Oracle Database")
        
        if not os.path.exists(video_path):
            print(f"Path not found: {video_path}")
            sys.exit(1)
        
        # Process videos
        if os.path.isfile(video_path):
            process_video(connection, video_path)
        else:
            # Process all video files in the directory
            video_extensions = ('.mp4', '.avi', '.mov', '.mkv', '.webm')
            for filename in os.listdir(video_path):
                if filename.lower().endswith(video_extensions):
                    video_file_path = os.path.join(video_path, filename)
                    process_video(connection, video_file_path)
                    
    finally:
        if 'connection' in locals():
            connection.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Store video embeddings')
    parser.add_argument('video_path', help='Path to video file or folder')
    
    args = parser.parse_args()
    store_video_embeddings(args.video_path) 