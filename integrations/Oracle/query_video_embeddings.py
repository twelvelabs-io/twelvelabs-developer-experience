import os
import sys
import array
import oracledb
from twelvelabs import TwelveLabs
from twelvelabs.models.embed import EmbeddingsTask

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


def similarity_search(connection, query_text):
    """Perform similarity search using query text"""
    try:
        # Create embedding for query
        embedding = twelvelabs_client.embed.create(
            model_name=EMBEDDING_MODEL,
            text=query_text,
            text_truncate="start",
        )
        
        if len(embedding.text_embedding.segments) > 1:
            print(f"Warning: Query generated {len(embedding.text_embedding.segments)} segments. Using only the first segment.")
        
        query_vector = array.array("d", embedding.text_embedding.segments[0].embeddings_float)
        
        # Search query
        search_sql = """
        SELECT video_file, start_time, end_time
        FROM video_embeddings
        ORDER BY vector_distance(embedding_vector, :1, COSINE)
        FETCH FIRST :2 ROWS ONLY
        """
        
        results = []
        cursor = connection.cursor()
        cursor.execute(search_sql, [query_vector, TOP_K])
        for row in cursor:
            results.append({
                'video_file': row[0],
                'start_time': row[1],
                'end_time': row[2]
            })
        cursor.close()
        return results
        
    except oracledb.DatabaseError as e:
        print(f"Database error occurred: {str(e)}")
        # Re-raise the exception to be handled by the calling function
        raise
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        raise

def similarity_search_multiple(connection, query_texts, batch_size=1000):
    """Perform multiple similarity searches using a list of query texts in batches"""
    results_by_query = {}
    
    # Process queries in batches
    for i in range(0, len(query_texts), batch_size):
        batch_queries = query_texts[i:i + batch_size]
        print(f"\nProcessing batch {i//batch_size + 1} ({len(batch_queries)} queries)")
        
        # Create embeddings for batch queries
        embeddings = []
        for query_text in batch_queries:
            embedding = twelvelabs_client.embed.create(
                model_name=EMBEDDING_MODEL,
                text=query_text,
                text_truncate="start",
            )
            
            if len(embedding.text_embedding.segments) > 1:
                print(f"Warning: Query '{query_text}' generated {len(embedding.text_embedding.segments)} segments. Using only the first segment.")
            
            query_vector = array.array("d", embedding.text_embedding.segments[0].embeddings_float)
            embeddings.append(query_vector)
        
        # Search query
        search_sql = """
        SELECT video_file, start_time, end_time
        FROM video_embeddings
        ORDER BY vector_distance(embedding_vector, :1, COSINE)
        FETCH FIRST :2 ROWS ONLY
        """
        
        with connection.cursor() as cursor:
            for query_text, query_vector in zip(batch_queries, embeddings):
                results = []
                for row in cursor.execute(search_sql, [query_vector, TOP_K]):
                    results.append({
                        'video_file': row[0],
                        'start_time': row[1],
                        'end_time': row[2]
                    })
                results_by_query[query_text] = results
    
    return results_by_query

def query_video_embeddings(query_text):
    """Query video embeddings database with the given text"""
    try:
        connection = oracledb.connect(
            user=db_username,
            password=db_password,
            dsn=db_connect_string,
            config_dir=db_wallet_path,
            wallet_location=db_wallet_path,
            wallet_password=db_password
        )
        
        # Verify DB version
        db_version = tuple(int(s) for s in connection.version.split("."))[:2]
        if db_version < (23, 7):
            sys.exit("This example requires Oracle Database 23.7 or later")
        print("Connected to Oracle Database")
        
        print("\nSearching for relevant video segments...")
        results = similarity_search(connection, query_text)
        
        print("\nResults:")
        print("========")
        for r in results:
            print(f"Video: {r['video_file']}")
            print(f"Segment: {r['start_time']:.1f}s to {r['end_time']:.1f}s\n")
                
    finally:
        if 'connection' in locals():
            connection.close()
    
    return results

def query_video_embeddings_multiple(query_texts):
    """Query video embeddings database with multiple text queries"""
    try:
        connection = oracledb.connect(
            user=db_username,
            password=db_password,
            dsn=db_connect_string,
            config_dir=db_wallet_path,
            wallet_location=db_wallet_path,
            wallet_password=db_password
        )
        
        # Verify DB version
        db_version = tuple(int(s) for s in connection.version.split("."))[:2]
        if db_version < (23, 7):
            sys.exit("This example requires Oracle Database 23.7 or later")
        print("Connected to Oracle Database")
        
        print("\nSearching for relevant video segments...")
        results_by_query = similarity_search_multiple(connection, query_texts)
        
        print("\nResults:")
        print("========")
        for query_text, results in results_by_query.items():
            print(f"\nQuery: '{query_text}'")
            print("-" * (len(query_text) + 9))
            for r in results:
                print(f"Video: {r['video_file']}")
                print(f"Segment: {r['start_time']:.1f}s to {r['end_time']:.1f}s\n")
                
    finally:
        if 'connection' in locals():
            connection.close()
    
    return results_by_query

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python query_video_embeddings.py 'query1' 'query2' ...")
        sys.exit(1)
    
    queries = sys.argv[1:]
    query_video_embeddings_multiple(queries)