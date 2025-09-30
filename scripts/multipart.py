#!/usr/bin/env python3
"""
Multipart Asset Upload Script

This script handles the complete multipart upload flow:
1. Splits file into chunks
2. Creates upload session 
3. Uploads chunks to S3
4. Reports completed chunks
5. Monitors progress until completion

Usage:
    python multipart_upload.py --file video.mp4 --api-key tlk_xxx --filename "my-video.mp4"
"""

import argparse
import json
import os
import sys
import time
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from pathlib import Path


class MultipartUploadClient:
    def __init__(self, base_url: str = "https://api.twelvelabs.io/v1.3", api_key: str = None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()
        
        if api_key:
            self.session.headers.update({
                'x-api-key': api_key
            })

    def split_file(self, file_path: str, chunk_size: int) -> List[str]:
        """Split file into chunks and return list of chunk file paths"""
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        file_size = file_path.stat().st_size
        print(f"📁 Splitting {file_path.name} ({file_size:,} bytes) into {chunk_size:,} byte chunks...")
        
        chunk_files = []
        chunk_dir = file_path.parent / f"{file_path.stem}_chunks"
        chunk_dir.mkdir(exist_ok=True)
        
        with open(file_path, 'rb') as f:
            chunk_num = 1
            while True:
                chunk_data = f.read(chunk_size)
                if not chunk_data:
                    break
                    
                chunk_file = chunk_dir / f"chunk_{chunk_num:04d}"
                with open(chunk_file, 'wb') as chunk_f:
                    chunk_f.write(chunk_data)
                
                chunk_files.append(str(chunk_file))
                print(f"  📄 Created chunk {chunk_num}: {len(chunk_data):,} bytes")
                chunk_num += 1
        
        print(f"✅ Split into {len(chunk_files)} chunks")
        return chunk_files, file_size

    def create_upload_session(self, filename: str, file_type: str, total_size: int) -> Dict:
        """Create multipart upload session"""
        print(f"🚀 Creating upload session for {filename}...")
        
        url = f"{self.base_url}/assets/multipart-uploads"
        data = {
            "filename": filename,
            "type": file_type,
            "total_size": total_size
        }
        
        response = self.session.post(url, json=data)
        response.raise_for_status()
        
        result = response.json()
        print(f"✅ Upload session created:")
        print(f"   Upload ID: {result['upload_id']}")
        print(f"   Asset ID: {result['asset_id']}")
        print(f"   Total chunks: {result['total_chunks']}")
        print(f"   Chunk size: {result['chunk_size']:,} bytes")
        print(f"   Initial URLs: {len(result['upload_urls'])}")
        
        return result

    def upload_chunk_to_s3(self, chunk_file: str, presigned_url: str) -> str:
        """Upload a single chunk to S3 and return ETag"""
        chunk_name = Path(chunk_file).name
        chunk_size = Path(chunk_file).stat().st_size
        
        print(f"📤 Uploading {chunk_name} ({chunk_size:,} bytes)...")
        
        with open(chunk_file, 'rb') as f:
            response = requests.put(
                presigned_url,
                data=f,
                headers={'Content-Type': 'application/octet-stream'}
            )
        
        response.raise_for_status()
        
        # Extract ETag (remove quotes if present)
        etag = response.headers.get('ETag', '').strip('"')
        print(f"✅ {chunk_name} uploaded, ETag: {etag}")
        
        return etag

    def get_additional_urls(self, upload_id: str, page: int = 1, limit: int = 10) -> List[Dict]:
        """Get additional presigned URLs if needed"""
        print(f"🔗 Getting additional URLs for upload {upload_id}...")
        
        url = f"{self.base_url}/assets/multipart-uploads/{upload_id}/presigned-urls"
        data = {'page': page, 'limit': limit}
        
        response = self.session.post(url, json=data)
        response.raise_for_status()
        
        result = response.json()
        urls = result.get('upload_urls', [])
        print(f"✅ Got {len(urls)} additional URLs")
        
        return urls

    def report_completed_chunks(self, upload_id: str, completed_chunks: List[Dict]) -> Dict:
        """Report completed chunks to the backend"""
        print(f"📋 Reporting {len(completed_chunks)} completed chunks...")
        
        url = f"{self.base_url}/assets/multipart-uploads/{upload_id}"
        data = {"completed_chunks": completed_chunks}
        
        response = self.session.post(url, json=data)
        response.raise_for_status()
        
        result = response.json()
        print(f"✅ Reported chunks:")
        print(f"   Processed: {result['processed_chunks']}")
        print(f"   Duplicates: {result['duplicate_chunks']}")
        print(f"   Total completed: {result['total_completed']}")
        
        if result.get('url'):
            print(f"🎉 Upload complete! Asset URL: {result['url'][:100]}...")
        
        return result

    def get_upload_status(self, upload_id: str) -> Dict:
        """Get current upload status"""
        url = f"{self.base_url}/assets/multipart-uploads/{upload_id}"
        params = {'page': 1, 'limit': 50}
        
        response = self.session.get(url, params=params)
        response.raise_for_status()
        
        return response.json()

    def cleanup_chunks(self, chunk_files: List[str]):
        """Clean up temporary chunk files"""
        print("🧹 Cleaning up chunk files...")
        for chunk_file in chunk_files:
            try:
                os.remove(chunk_file)
            except OSError:
                pass
        
        # Remove chunk directory if empty
        if chunk_files:
            chunk_dir = Path(chunk_files[0]).parent
            try:
                chunk_dir.rmdir()
            except OSError:
                pass
        
        print("✅ Cleanup complete")

    def upload_file(self, file_path: str, filename: str = None, file_type: str = "video", 
                   batch_size: int = 10) -> str:
        """
        Complete multipart upload flow
        
        Args:
            file_path: Path to file to upload
            filename: Name to use for the asset (defaults to file basename)
            file_type: Asset type (default: "video")
            batch_size: Number of chunks to report in each batch (default: 4)
            
        Returns:
            Final asset URL
        """
        file_path = Path(file_path)
        if filename is None:
            filename = file_path.name
            
        try:
            # Step 1: Get file size first
            total_size = file_path.stat().st_size
            print(f"📁 File size: {total_size:,} bytes")
            
            # Step 2: Create upload session
            upload_session = self.create_upload_session(filename, file_type, total_size)
            upload_id = upload_session['upload_id']
            chunk_size = upload_session['chunk_size']

            # Step 3: Split file using chunk size from upload session
            chunk_files, _ = self.split_file(str(file_path), chunk_size)
            
            # Step 3: Upload all chunks in parallel batches
            current_urls = {url['chunk_index']: url['url'] for url in upload_session['upload_urls']}
            print(f"🔗 Initial URLs available: {len(current_urls)}")
            
            # Process chunks in batches with parallel uploads
            total_chunks = len(chunk_files)
            for batch_start in range(0, total_chunks, batch_size):
                batch_end = min(batch_start + batch_size, total_chunks)
                batch_chunk_files = chunk_files[batch_start:batch_end]
                batch_indices = list(range(batch_start + 1, batch_end + 1))  # 1-based indexing
                
                print(f"📦 Processing batch: chunks {batch_indices[0]}-{batch_indices[-1]} ({len(batch_chunk_files)} chunks)")
                
                # Ensure we have URLs for all chunks in this batch
                missing_urls = [idx for idx in batch_indices if idx not in current_urls]
                if missing_urls:
                    print(f"🔗 Need URLs for chunks: {missing_urls}")
                    # Get additional URLs for missing chunks
                    for chunk_index in missing_urls:
                        page = (chunk_index - 1) // 10 + 1
                        additional_urls = self.get_additional_urls(upload_id, page=page)
                        for url_info in additional_urls:
                            current_urls[url_info['chunk_index']] = url_info['url']
                
                # Verify all URLs are available
                for chunk_index in batch_indices:
                    if chunk_index not in current_urls:
                        raise Exception(f"No presigned URL available for chunk {chunk_index}")
                
                # Upload batch chunks in parallel
                completed_chunks = []
                max_workers = min(len(batch_chunk_files), 5)  # Limit concurrent uploads
                
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    # Submit all uploads in the batch
                    future_to_chunk = {}
                    for i, chunk_file in enumerate(batch_chunk_files):
                        chunk_index = batch_indices[i]
                        presigned_url = current_urls[chunk_index]
                        future = executor.submit(self.upload_chunk_to_s3, chunk_file, presigned_url)
                        future_to_chunk[future] = (chunk_index, chunk_file)
                    
                    # Collect results as they complete
                    for future in as_completed(future_to_chunk):
                        chunk_index, chunk_file = future_to_chunk[future]
                        try:
                            etag = future.result()
                            chunk_size_bytes = Path(chunk_file).stat().st_size
                            completed_chunks.append({
                                "chunk_index": chunk_index,
                                "proof": etag,
                                "proof_type": "etag", 
                                "chunk_size": chunk_size_bytes
                            })
                            print(f"✅ Chunk {chunk_index} completed in batch")
                        except Exception as e:
                            print(f"❌ Chunk {chunk_index} failed: {e}")
                            raise
                
                # Report completed batch
                print(f"📋 Reporting batch of {len(completed_chunks)} chunks...")
                result = self.report_completed_chunks(upload_id, completed_chunks)
                
                # Check if upload is complete
                if result.get('url'):
                    print(f"🎉 Upload completed successfully!")
                    return result['url']
                
                print(f"📊 Progress: {batch_end}/{total_chunks} chunks uploaded")
            
            # Final status check
            print("⏳ Checking final status...")
            status = self.get_upload_status(upload_id)
            print(f"📊 Final status: {status['status']}")
            print(f"   Completed: {status['chunks_completed']}/{status['total_chunks']}")
            
            if status['status'] == 'completed':
                # Get the asset URL by checking the asset endpoint
                asset_id = upload_session['asset_id']
                asset_url = self.get_asset_url(asset_id)
                return asset_url
            else:
                raise Exception(f"Upload not completed. Status: {status['status']}")
                
        finally:
            # Cleanup temporary files
            if 'chunk_files' in locals():
                self.cleanup_chunks(chunk_files)

    def get_asset_url(self, asset_id: str) -> str:
        """Get the final asset URL"""
        url = f"{self.base_url}/assets/{asset_id}"
        response = self.session.get(url)
        response.raise_for_status()
        
        result = response.json()
        return result.get('url', '')


def main():
    parser = argparse.ArgumentParser(description='Upload file using multipart upload')
    parser.add_argument('--file', required=True, help='Path to file to upload')
    parser.add_argument('--api-key', required=True, help='API key for authentication')
    parser.add_argument('--filename', help='Filename to use for asset (default: use file basename)')
    parser.add_argument('--type', default='video', help='Asset type (default: video)')
    parser.add_argument('--base-url', default='https://api.twelvelabs.io/v1.3', help='Base URL of the API')
    parser.add_argument('--batch-size', type=int, default=10, help='Chunks to report per batch (default: 4)')
    
    args = parser.parse_args()
    
    try:
        client = MultipartUploadClient(args.base_url, args.api_key)
        
        print("🚀 Starting multipart upload...")
        print(f"   File: {args.file}")
        print(f"   Batch size: {args.batch_size}")
        print()
        
        start_time = time.time()
        asset_url = client.upload_file(
            args.file, 
            args.filename, 
            args.type,
            args.batch_size
        )
        end_time = time.time()
        
        print(f"\n🎉 Upload completed in {end_time - start_time:.1f} seconds!")
        print(f"🔗 Asset URL: {asset_url}")
        
    except KeyboardInterrupt:
        print("\n⚠️  Upload cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
