# Twelve Labs Oracle DB Integration

This guide explains how to use Twelve Labs video embeddings with Oracle Database 23.7 or later. The integration allows you to store and query video embeddings using Oracle's vector similarity search capabilities.

## Prerequisites

- Oracle Database 23.7 or later
- Python 3.x
- Oracle Client libraries
- Twelve Labs API Key

## Installation

```bash
pip3 install oracledb
pip3 install twelvelabs
```

## Create an Oracle Autonomous Database

0. Create an [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/) account
1. Navigate to [Oracle Cloud Console](https://cloud.oracle.com/db/adb)
2. Click "Create Autonomous Database"
3. Configure your database:
   - Display name: Choose a meaningful name (e.g., "VideoEmbeddingsDB")
   - Database name: Provide a unique identifier (max 14 characters)
   - Workload type: Select "Data Warehouse"
   - Deployment type: Choose "Serverless"
   - Select "Always Free"
   - Database version: Select 23ai

4. Set administrator credentials:
   - Password must be 12-30 characters and include uppercase, lowercase, numbers, and special characters

5. Network access:
   - Choose "Allow secure access from everywhere"
   - Or configure a private endpoint for production environments

6. Click "Create Autonomous Database"

7. Download the wallet file:
   - Click on your new database
   - Select "Database connection"
   - Click "Download wallet"
   - Save the wallet to a secure location
   - This wallet path will be used in ORACLE_DB_WALLET_PATH environment variable


## Set Environment Variables

Set the following environment variables:

```bash
export ORACLE_DB_USERNAME=your_username
export ORACLE_DB_PASSWORD=your_password
export ORACLE_DB_CONNECT_STRING=your_connect_string
export ORACLE_DB_WALLET_PATH=/path/to/wallet
export TWELVE_LABS_API_KEY=your_api_key
```


## Setup Database Schema and Index

First, create the database schema and index using `create_schema_video_embeddings.py`

This will
1. Drop the `video_embeddings` table if it already exists
2. Create a new `video_embeddings` table
3. Create a vector index 

Run the schema and index creation script:

```bash
python3 create_schema_video_embeddings.py
```

## Store Video Embeddings

Use `store_video_embeddings.py` to process videos and store their embeddings. The script can handle both individual video files and directories containing multiple videos.

```bash
# For a single video file
python3 store_video_embeddings.py /path/to/video.mp4

# For a directory of videos
python3 store_video_embeddings.py /path/to/video/directory
```

## Query Video Embeddings

Use `query_video_embeddings.py` to search for relevant video segments using natural language queries.

Usage:

Enter as many queries as you would like.

```bash
python3 query_video_embeddings.py "query one" "query two"
```

The script will:
1. Convert your text query into an embedding using Twelve Labs Marengo model
2. Search the database using cosine similarity
3. Return the top 5 most relevant video segments with their timestamps

Example output:
```
Connected to Oracle Database

Searching for relevant video segments...

Results:
========
Video: lecture1.mp4
Segment: 15.0s to 21.0s

Video: lecture2.mp4
Segment: 45.5s to 51.5s
```
