from __future__ import annotations

from typing import AsyncIterable
from enum import Enum

import fastapi_poe as fp
from modal import App, Image, asgi_app, Volume, Secret, web_endpoint

from twelvelabs import TwelveLabs
import twelvelabs
import time
import os
import json
import fcntl
import asyncio
import logging
import mimetypes
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile
import aiohttp
import math
import hashlib
import shutil

from fastapi import FastAPI
from fastapi.responses import FileResponse

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Enable test mode with reduced pricing
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"
PRICE_REDUCTION_FACTOR = 100 if TEST_MODE else 1

# Video duration constraints (in seconds)
MIN_VIDEO_DURATION = 4
MAX_VIDEO_DURATION = 1200  # 20 minutes

# Base costs in millicents (1/1000th of a cent)
BASE_VIDEO_INDEXING_COST = 3300  # $0.33 per minute
BASE_AUDIO_INDEXING_COST = 830   # $0.0083 per minute
BASE_STORAGE_COST = 150          # $0.0015 per minute
BASE_INPUT_TOKEN_COST = 0.1      # $0.001 / 1000 tokens
BASE_OUTPUT_TOKEN_COST = 0.2     # $0.002 / 1000 tokens

OUTPUT_TEXT_TOKEN_ASSUMPTION = 1000

# Actual costs after applying test mode reduction
TWELVE_LABS_VIDEO_INDEXING_COST = BASE_VIDEO_INDEXING_COST // PRICE_REDUCTION_FACTOR
TWELVE_LABS_AUDIO_INDEXING_COST = BASE_AUDIO_INDEXING_COST // PRICE_REDUCTION_FACTOR
TWELVE_LABS_STORAGE_COST = BASE_STORAGE_COST // PRICE_REDUCTION_FACTOR
TWELVE_LABS_INPUT_TOKEN_COST = BASE_INPUT_TOKEN_COST / PRICE_REDUCTION_FACTOR
TWELVE_LABS_OUTPUT_TOKEN_COST = BASE_OUTPUT_TOKEN_COST / PRICE_REDUCTION_FACTOR

FREE_VIDEO_DURATION_MINUTES = 1

logger.info(f"Running in {'TEST' if TEST_MODE else 'PRODUCTION'} mode")
logger.info(f"Price reduction factor: {PRICE_REDUCTION_FACTOR}x")
logger.info(f"Video indexing cost: {TWELVE_LABS_VIDEO_INDEXING_COST} millicents per minute")
logger.info(f"Audio indexing cost: {TWELVE_LABS_AUDIO_INDEXING_COST} millicents per minute")
logger.info(f"Storage cost: {TWELVE_LABS_STORAGE_COST} millicents per minute")
logger.info(f"Input token cost: {TWELVE_LABS_INPUT_TOKEN_COST} millicents per 1000 tokens")
logger.info(f"Output token cost: {TWELVE_LABS_OUTPUT_TOKEN_COST} millicents per 1000 tokens")

class VideoUrlType(Enum):
    DIRECT = "direct"
    YOUTUBE = "youtube"
    INVALID = "invalid"
    NOT_URL = "not_url"

class VideoDurationError(Exception):
    """Custom exception for video duration validation"""
    pass

class TwelveLabsBot(fp.PoeBot):

    """
    This PoeBot allows a user to upload a video and ask quesions about that video.
    The user can only ask questions about the most recently uploaed video.
    The user can ask as many questions as they want.
    """

    # Add to class constants
    INSUFFICIENT_FUNDS_MESSAGE = (
        "⚠️ Sorry, you don't have enough points to process this request.\n\n"
        "Please check the rate card above for costs.\n\n"
        "💡 Tip: Videos under 1 minute are free! Try uploading a shorter clip or add more points to continue."
    )

    # Standardize all message constants at class level
    VIDEO_TOO_SHORT_MESSAGE = f"The video is too short. Please use a video that's at least {MIN_VIDEO_DURATION} seconds long."
    VIDEO_TOO_LONG_MESSAGE = f"The video is too long. Please use a video that's no longer than {MAX_VIDEO_DURATION//60} minutes."
    PROCESSING_COMPLETE_MESSAGE = "\n\nVideo processing is complete! You can now ask questions about the video. What would you like to know?"
    PROCESSING_STARTED_MESSAGE = "Video processing started! This may take a few minutes..."
    STILL_PROCESSING_MESSAGE = "Your video is still processing. This may take a few minutes..."
    CHECK_BACK_MESSAGE = "\n\nVideo processing is taking longer than expected. Your video is still being processed - please check back in a few seconds with any message."
    PROCESSING_FAILED_MESSAGE = "Sorry, there was an error processing your video. Please try again."
    NO_TASK_MESSAGE = "No processing task found. Please upload a video to get started."
    YOUTUBE_ERROR_MESSAGE = (
        "I apologize, but I cannot process YouTube links. Please either:\n\n"
        "1. Provide a direct video file URL (ending in .mp4, .mov, etc.)\n"
        "2. Upload a video file directly\n\n"
        "YouTube links are not supported at this time."
    )
    INVALID_URL_MESSAGE = (
        "The URL you provided doesn't appear to be a direct link to a video file. Please make sure:\n\n"
        "1. The URL starts with http:// or https://\n"
        "2. The URL ends with a video file extension (like .mp4, .mov, etc.)\n\n"
        "Alternatively, you can upload a video file directly."
    )
    UPLOAD_INSTRUCTIONS_MESSAGE = (
        f"To get started, please share a video that's {MIN_VIDEO_DURATION} seconds - {MAX_VIDEO_DURATION//60} minutes long. "
        "You can either:\n\n"
        "1. Upload a video file directly\n"
        "2. Share a direct video file URL (MP4, MOV, etc. - YouTube and Dropbox links are not supported)\n\n"
        "Once you share a video, I'll process it and then you can ask me questions about its content!"
    )
    WRONG_FILE_TYPE_MESSAGE = "Sorry, I can only process video files. The file you sent appears to be a {content_type} file."
    PENDING_QUESTION_RESPONSE = "I noticed you asked: '{question}'\n\nHere's my response: {answer}"
    STILL_ASK_QUESTIONS_MESSAGE = "\n\nYou can still ask questions about your previous video if you'd like!"
    GENERATING_HIGHLIGHTS_TEXT = "\n\nGenerating highlight clips...\n\n"
    GENERATING_CHAPTERS_TEXT = "\n\nGenerating chapter clips...\n\n"

    def __init__(self, api_key, storage_dir):
        super().__init__()
        self.client = TwelveLabs(api_key=api_key)
        self.storage_dir = storage_dir
        # Create segments directory
        self.segments_dir = Path(storage_dir) / "segments"
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Initialized TwelveLabsBot with storage directory: {storage_dir}")

    def _get_user_data_path(self, user_id):
        return os.path.join(self.storage_dir, f"user_{user_id}.json")
        
    def _load_user_data(self, user_id):
        path = self._get_user_data_path(user_id)
        default_data = {
            "video_id": None, 
            "index_id": None,
            "processing_task": None,
            "processing_url": None,
            "conversation_id": None,
            "pending_question": None,
            "processed_videos": {}  # Add this to store video URL/hash -> video_id mapping
        }
        
        if os.path.exists(path):
            logger.debug(f"Loading user data for user_id: {user_id}")
            try:
                with open(path, 'r') as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        loaded_data = json.load(f)
                        default_data.update(loaded_data)
                        return default_data
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse user data JSON: {e}")
                        return default_data
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except IOError as e:
                logger.error(f"Failed to read user data file: {e}")
                return default_data
        logger.debug(f"Using default data for user_id: {user_id}")
        return default_data
        
    def _save_user_data(self, user_id, data):
        path = self._get_user_data_path(user_id)
        try:
            os.makedirs(self.storage_dir, exist_ok=True)
            with open(path, 'w') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(data, f)
                except (TypeError, ValueError) as e:
                    logger.error(f"Failed to serialize user data: {e}")
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except IOError as e:
            logger.error(f"Failed to write user data file: {e}")

    

    async def get_settings(self, setting: fp.SettingsRequest) -> fp.SettingsResponse:
        rate_card = (
            "# 🎥 Video Processing Costs\n\n"
            "Videos under 1 minute are **FREE**! For longer videos:\n\n"
            "| Service | Cost per Minute |\n"
            "|---------|----------------|\n"
            f"| Visual Analysis | [amount_usd_milli_cent={TWELVE_LABS_VIDEO_INDEXING_COST}] |\n"
            f"| Audio Analysis | [amount_usd_milli_cent={TWELVE_LABS_AUDIO_INDEXING_COST}] |\n"
            f"| Storage | [amount_usd_milli_cent={TWELVE_LABS_STORAGE_COST}] |\n\n"
            "# 💬 Question Costs\n\n"
            "Each question about your video has two components:\n\n"
            "| Component | Cost per 1000 Tokens |\n"
            "|-----------|------------------|\n"
            f"| Your Question | [amount_usd_milli_cent={math.ceil(TWELVE_LABS_INPUT_TOKEN_COST * 1000)}] |\n"
            f"| AI Response | [amount_usd_milli_cent={math.ceil(TWELVE_LABS_OUTPUT_TOKEN_COST * 1000)}] |\n\n"
            "💡 **Tips to Save Points:**\n"
            "1. Upload shorter videos (under 1 minute is free!)\n"
            "2. Ask concise questions\n"
            "3. Use commands like 'summary', 'chapters' or 'highlights' for efficient overviews"
        )
        
        return fp.SettingsResponse(
            allow_attachments=True,
            custom_rate_card=rate_card,
            introduction_message=(
                "Hi! I'm Pegasus! 👋 I can help you understand videos just like a human would! "
                "To get started, please share a video that's 4 seconds - 20 minutes long - you can upload a file or share a URL. "
                "**Note that I can't accept YouTube URLs or other non-publicly available URLs at this point.**\n\n"
                "💡 Tip: Videos under 1 minute are free!\n\n"
                "Once your video is processed, here's how you can interact with it:\n"
                "1. Ask open ended questions about the video\n"
                "2. Say \"Summarize\" or \"Summary\" to generate a summary of your video\n"
                "3. Say \"Chapter\" to break the video down by chapter and see a summary of each chapter\n"
                "4. Say \"Highlight\" to generate a list of highlights for your video\n\n"
                "Let's explore your videos together!"
            )
        )


    def is_valid_video_url(self, url):
        """
        Check if the given URL is a valid video URL.
        Returns VideoUrlType enum indicating the type of video URL.
        """
        # Skip URL validation if the input is too short to be a URL
        # or doesn't contain a dot (all URLs must have at least one dot)
        if len(url) < 4 or '.' not in url:
            return VideoUrlType.NOT_URL
        
        # Check if the URL starts with http:// or https://
        if not (url.startswith('http://') or url.startswith('https://')):
            return VideoUrlType.NOT_URL
        
        # Check for YouTube URLs
        youtube_patterns = [
            'youtube.com',
            'youtu.be'
        ]
        if any(pattern in url.lower() for pattern in youtube_patterns):
            return VideoUrlType.YOUTUBE
        
        # Twelve Labs supports all ffmpeg supported formats
        video_extensions = [
            '.mp4', '.mov', '.avi', '.wmv', '.flv', '.webm', '.mkv',
            '.3gp', '.3g2', '.mj2', '.asf', '.dv', '.f4v', '.gif',
            '.m2ts', '.m2v', '.m4v', '.mjpeg', '.mpg', '.mpeg',
            '.mts', '.mxf', '.ogv', '.rm', '.rmvb', '.ts', '.vob'
        ]
        
        # Check if the URL ends with any of the supported extensions
        if any(url.lower().endswith(ext) for ext in video_extensions):
            return VideoUrlType.DIRECT
        
        return VideoUrlType.INVALID


    async def get_response(
        self, request: fp.QueryRequest
    ) -> AsyncIterable[fp.PartialResponse]:
        
        yield fp.PartialResponse(
            text="Summarize this video.",
            is_suggested_reply=True
        )
        yield fp.PartialResponse(
            text="Generate highlights.",
            is_suggested_reply=True
        )
        yield fp.PartialResponse(
            text="Generate chapter summaries.",
            is_suggested_reply=True
        )

        logger.info(f"Received request for conversation: {request.conversation_id}")
        
        # Load user data at start of response
        user_data = self._load_user_data(request.conversation_id)
        user_data["conversation_id"] = request.conversation_id
        self._save_user_data(request.conversation_id, user_data)
        
        # Use user_data instead of instance variables
        video_id = user_data["video_id"]
        index_id = user_data["index_id"]
        
        last_message = request.query[-1].content
        
        # Fix the incorrect logging statements
        logger.info(f"User input: {last_message}")
        logger.info(f"Query: {request.query[-1]}")

        #check if there is an attachment to upload
        is_attachment = len(request.query[-1].attachments) > 0

        video_url_type = self.is_valid_video_url(last_message)
        if video_url_type == VideoUrlType.YOUTUBE:
            response = self.YOUTUBE_ERROR_MESSAGE
            if user_data.get("video_id"):
                response += self.STILL_ASK_QUESTIONS_MESSAGE
            logger.info(response)
            yield fp.PartialResponse(text=response)
            return
        elif video_url_type == VideoUrlType.INVALID:
            # Check if this message contains both a URL and a question
            if any(word.startswith('http') for word in last_message.split()):
                # Save the non-URL parts as pending question
                question = ' '.join(word for word in last_message.split() if not word.startswith('http'))
                if question.strip():
                    user_data["pending_question"] = question.strip()
                    self._save_user_data(request.conversation_id, user_data)
                    logger.info(f"Saved pending question: {question.strip()}")
            
            response = self.INVALID_URL_MESSAGE
            if user_data.get("video_id"):
                response += self.STILL_ASK_QUESTIONS_MESSAGE
            logger.info(response)
            yield fp.PartialResponse(text=response)
            return
        elif video_url_type == VideoUrlType.DIRECT:
            # Save any text that comes before/after the URL as pending question
            question = ' '.join(word for word in last_message.split() if not word.startswith('http'))
            if question.strip():
                user_data["pending_question"] = question.strip()
                self._save_user_data(request.conversation_id, user_data)
                logger.info(f"Saved pending question: {question.strip()}")

            logger.info(f"Processing video URL: {last_message}")
            self.video_url = last_message
            
            try:
                # Create task and wait for processing
                task_id = await self.create_pegasus_video_task(request.conversation_id, request)
                
                if isinstance(task_id, tuple) and task_id[0] == "error":
                    # Show our custom error message
                    yield fp.PartialResponse(text=task_id[1])
                    # Clear any pending questions since we couldn't process the video
                    user_data = self._load_user_data(request.conversation_id)
                    user_data["pending_question"] = None
                    self._save_user_data(request.conversation_id, user_data)
                    return
                elif task_id == "insufficient_fund":
                    yield fp.ErrorResponse(
                        text=(
                            "⚠️ Sorry, you don't have enough points to process this video.\n\n"
                            "Please check the rate card above for video processing costs.\n\n"
                            "💡 Tip: Videos under 1 minute are free! Try uploading a shorter clip."
                        ),
                        error_type="insufficient_fund"
                    )
                    # Clear any pending questions since we couldn't process the video
                    user_data = self._load_user_data(request.conversation_id)
                    user_data["pending_question"] = None
                    self._save_user_data(request.conversation_id, user_data)
                    return
                elif task_id is None:
                    # Video was already processed, continue with existing video_id
                    async for status in self.wait_for_processing(request.conversation_id, request):
                        yield status
                else:
                    # Get the upload message with time estimate
                    user_data = self._load_user_data(request.conversation_id)
                    upload_message = user_data.get("upload_message", "Video uploaded, processing started! This may take a few minutes...")
                    
                    logger.info(f"Sending response: {upload_message}")
                    yield fp.PartialResponse(text=upload_message)
                    await asyncio.sleep(1)
                    
                    # New video being processed
                    async for status in self.wait_for_processing(request.conversation_id, request):
                        yield status

            except Exception as e:
                logger.error(f"Error processing video: {e}")
                response = self.PROCESSING_FAILED_MESSAGE
                if user_data.get("video_id"):
                    response += self.STILL_ASK_QUESTIONS_MESSAGE
                yield fp.PartialResponse(text=response)
            return
    
        elif is_attachment:
            attachment = request.query[-1].attachments[0]
            if "video" in attachment.content_type:
                # Save any text message that came with the attachment
                if last_message.strip():
                    user_data["pending_question"] = last_message.strip()
                    self._save_user_data(request.conversation_id, user_data)
                    logger.info(f"Saved pending question: {last_message.strip()}")

                logger.info(f"Processing video attachment: {attachment.name}")
                self.video_url = attachment.url 
                self.video_name = attachment.name
                logger.info(f"Video filename: {attachment.name}")
                
                try:
                    # Create task and check funds first
                    result = await self.create_pegasus_video_task(request.conversation_id, request)
                    
                    if isinstance(result, tuple) and result[0] == "error":
                        # Show our custom error message
                        yield fp.PartialResponse(text=result[1])
                        # Clear any pending questions since we couldn't process the video
                        user_data = self._load_user_data(request.conversation_id)
                        user_data["pending_question"] = None
                        self._save_user_data(request.conversation_id, user_data)
                        return
                    elif result == "insufficient_fund":
                        yield fp.ErrorResponse(
                            text=(
                                "⚠️ Sorry, you don't have enough points to process this video.\n\n"
                                "Please check the rate card above for video processing costs.\n\n"
                                "💡 Tip: Videos under 1 minute are free! Try uploading a shorter clip."
                            ),
                            error_type="insufficient_fund"
                        )
                    task_id = result  # If not an error, it's the task_id
                    # Get the upload message with time estimate
                    user_data = self._load_user_data(request.conversation_id)
                    upload_message = user_data.get("upload_message", "Video uploaded, processing started! This may take a few minutes...")
                    
                    logger.info(f"Sending response: {upload_message}")
                    yield fp.PartialResponse(text=upload_message)
                    await asyncio.sleep(1)

                    if task_id is None:
                        # Video was already processed, continue with existing video_id
                        async for status in self.wait_for_processing(request.conversation_id, request):
                            yield status
                    else:
                        # New video being processed
                        async for status in self.wait_for_processing(request.conversation_id, request):
                            yield status

                except Exception as e:
                    logger.error(f"Error processing video: {e}")
                    response = self.PROCESSING_FAILED_MESSAGE
                    if user_data.get("video_id"):
                        response += self.STILL_ASK_QUESTIONS_MESSAGE
                    yield fp.PartialResponse(text=response)
                return
            else:
                response = self.WRONG_FILE_TYPE_MESSAGE.format(content_type=attachment.content_type)
                if user_data.get("video_id"):
                    response += self.STILL_ASK_QUESTIONS_MESSAGE
                logger.info(f"Received non-video attachment: {attachment.content_type}")
                yield fp.PartialResponse(text=response)
                return

        elif user_data.get("processing_task"):
            # Any message while processing should check status
            task = self.client.task.retrieve(user_data["processing_task"])
            if task.status == "ready":
                # Save video_id if not already saved
                if not user_data.get("video_id"):
                    user_data["video_id"] = task.video_id
                    user_data["processing_task"] = None
                    self._save_user_data(request.conversation_id, user_data)
                logger.info(f"Sending response: {self.PROCESSING_COMPLETE_MESSAGE}")
                yield fp.PartialResponse(text=self.PROCESSING_COMPLETE_MESSAGE)
            else:
                logger.info(f"Sending response: {self.STILL_PROCESSING_MESSAGE}")
                yield fp.PartialResponse(text=self.STILL_PROCESSING_MESSAGE)
                await asyncio.sleep(1)
                async for status in self.wait_for_processing(request.conversation_id, request):
                    if status.text == "ready":
                        yield fp.PartialResponse(text=self.PROCESSING_COMPLETE_MESSAGE)
                        return
                    elif status.text == "timeout":
                        yield fp.PartialResponse(text=self.CHECK_BACK_MESSAGE)
                        return
                    else:
                        yield status
                return
            return

        elif video_id is None:
            logger.info(f"Sending response: {self.UPLOAD_INSTRUCTIONS_MESSAGE}")
            yield fp.PartialResponse(
                text=self.UPLOAD_INSTRUCTIONS_MESSAGE
            )

        else:
            #chat with video
            if "chapter" in last_message.lower():
                res = self.client.generate.summarize(
                    video_id=video_id,
                    type="chapter",
                )
                
                # Build formatted chapters text
                chapters_text = "Here are the video chapters:\n\n"
                for chapter in res.chapters.root:
                    timestamp = f"{chapter.start:.1f}s - {chapter.end:.1f}s"
                    chapters_text += f"Chapter {chapter.chapter_number + 1}: {chapter.chapter_title}\n"
                    chapters_text += f"Time: {timestamp}\n"
                    chapters_text += f"Summary: {chapter.chapter_summary}\n\n"
                
                success, error = await self.handle_text_generation_costs(request, last_message, chapters_text)
                if not success:
                    yield error
                    return

                # Send the chapters text
                yield fp.PartialResponse(text=chapters_text)

                print(chapters_text)

                time.sleep(0.1)

                yield fp.PartialResponse(text=self.GENERATING_CHAPTERS_TEXT)
                
                # Process video segments silently (no additional text)
                video_url = user_data.get("processing_url")
                if video_url:
                    for chapter in res.chapters.root:
                        segment_path = await self.extract_video_segment(
                            video_url, 
                            chapter.start, 
                            chapter.end
                        )
                        if segment_path:
                            try:
                                # Just post the video segment without any additional text
                                with open(segment_path, "rb") as f:
                                    file_data = f.read()
                                await self.post_message_attachment(
                                    message_id=request.message_id,
                                    file_data=file_data,
                                    filename=f"chapter_{chapter.chapter_number + 1}.mp4"
                                )
                            finally:
                                try:
                                    os.unlink(segment_path)
                                except Exception as e:
                                    logger.error(f"Failed to clean up video segment: {e}")

            elif "highlight" in last_message.lower():
                res = self.client.generate.summarize(
                    video_id=video_id,
                    type="highlight"
                )
                
                # First build the highlights text
                highlights_text = "Here are the video highlights:\n\n"
                for idx, highlight in enumerate(res.highlights.root, 1):
                    timestamp = f"{highlight.start:.1f}s - {highlight.end:.1f}s"
                    highlights_text += f"{idx}. {highlight.highlight} ({timestamp})\n"
                
                # Handle costs first
                success, error = await self.handle_text_generation_costs(request, last_message, highlights_text)
                if not success:
                    yield error
                    return
                
                yield fp.PartialResponse(text=highlights_text)

                print(highlights_text)
                time.sleep(0.1)

                yield fp.PartialResponse(text=self.GENERATING_HIGHLIGHTS_TEXT)

                time.sleep(0.1)
                
                # Process video segments silently (no additional text)
                video_url = user_data.get("processing_url")
                if video_url:
                    for idx, highlight in enumerate(res.highlights.root, 1):
                        segment_path = await self.extract_video_segment(
                            video_url, 
                            highlight.start, 
                            highlight.end
                        )
                        if segment_path:
                            try:
                                # Just post the video segment without any additional text
                                with open(segment_path, "rb") as f:
                                    file_data = f.read()
                                await self.post_message_attachment(
                                    message_id=request.message_id,
                                    file_data=file_data,
                                    filename=f"highlight_{idx}.mp4"
                                )
                            finally:
                                try:
                                    os.unlink(segment_path)
                                except Exception as e:
                                    logger.error(f"Failed to clean up video segment: {e}")

            elif "summarize" in last_message.lower() or "summary" in last_message.lower():
                res = self.client.generate.summarize(
                    video_id=video_id,
                    type="summary"
                )
                print(res)
                success, error = await self.handle_text_generation_costs(request, last_message, f"{res.summary}")
                if not success:
                    yield error
                    return
                response_text = f"{res.summary}"

                logger.info(f"Sending response: {response_text}")
                yield fp.PartialResponse(
                    text=response_text
                )

            else:
                res = self.client.generate.text(
                    video_id=video_id,
                    prompt=last_message
                )
                success, error = await self.handle_text_generation_costs(request, last_message, f"{res.data}")
                if not success:
                    yield error
                    return
                response_text = f"{res.data}"
            
            
                logger.info(f"Sending response: {response_text}")
                yield fp.PartialResponse(
                    text=response_text
                )

            yield fp.PartialResponse(
                text=""
            )
                    
    def on_task_update(self,task):
        print(f"  Status={task.status}")

    def format_duration(self, minutes):
        """Convert minutes to a concise MM:SS format"""
        total_seconds = int(minutes * 60)
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}:{seconds:02d} min"  # :02d ensures two digits with leading zero

    def millicents_to_points(self, millicents):
        """Convert millicents to Poe points (1 millicent = 1 point)"""
        return math.ceil(millicents)  # Just round up to nearest point

    async def create_pegasus_video_task(self, conversation_id, request):
        logger.info(f"Creating video processing task for conversation: {conversation_id}")
        downloaded_path = None
        processed_path = None
        
        try:
            # Load user data to check for previously processed videos
            user_data = self._load_user_data(conversation_id)
            processed_videos = user_data.get("processed_videos", {})

            # For direct URLs, first check if we've processed this URL before
            if not hasattr(self, 'video_name') and self.video_url in processed_videos:
                video_key = processed_videos[self.video_url]
                logger.info(f"URL already processed, reusing video_id: {video_key}")
                user_data["video_id"] = video_key
                user_data["processing_task"] = None
                self._save_user_data(conversation_id, user_data)
                return None  # Return None to indicate no new task needed

            # For uploaded files or new URLs, we need to download to check the hash
            downloaded_path = await self.download_video(self.video_url)
            logger.info(f"Video downloaded to: {downloaded_path}")

            # Generate hash of the video content
            with open(downloaded_path, 'rb') as f:
                video_key = hashlib.md5(f.read()).hexdigest()
            
            # Check if we've processed this video content before
            if video_key in processed_videos:
                logger.info(f"Video content already processed, reusing video_id: {processed_videos[video_key]}")
                user_data["video_id"] = processed_videos[video_key]
                user_data["processing_task"] = None
                # For URLs, store the URL mapping for future direct lookups
                if not hasattr(self, 'video_name'):
                    processed_videos[self.video_url] = video_key
                self._save_user_data(conversation_id, user_data)
                return None  # Return None to indicate no new task needed

            # If we get here, it's a new video. Calculate costs and proceed with processing
            try:
                duration_command = [
                    'ffprobe',
                    '-v', 'error',
                    '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1',
                    downloaded_path
                ]
                
                duration_result = subprocess.run(
                    duration_command,
                    check=True,
                    capture_output=True,
                    text=True
                )

                duration_minutes = float(duration_result.stdout) / 60
                duration_seconds = float(duration_result.stdout)
                
                # Calculate estimated processing time (15% of duration, rounded up to nearest minute)
                processing_minutes = math.ceil(duration_seconds * 0.15 / 60)
                processing_time_str = f"{processing_minutes} minute{'s' if processing_minutes != 1 else ''}"
                
                # Create upload message with time estimate
                upload_message = f"Video uploaded, processing started! This should take about {processing_time_str} to complete..."
                
                # Save to user data for use in get_response
                user_data = self._load_user_data(conversation_id)
                user_data["upload_message"] = upload_message
                self._save_user_data(conversation_id, user_data)
                
                # Validate video duration
                if duration_seconds < MIN_VIDEO_DURATION:
                    error_msg = self.VIDEO_TOO_SHORT_MESSAGE
                    if user_data.get("video_id"):
                        error_msg += self.STILL_ASK_QUESTIONS_MESSAGE
                    return ("error", error_msg)
                if duration_seconds > MAX_VIDEO_DURATION:
                    error_msg = self.VIDEO_TOO_LONG_MESSAGE
                    if user_data.get("video_id"):
                        error_msg += self.STILL_ASK_QUESTIONS_MESSAGE
                    return ("error", error_msg)
                
                duration_minutes_ceil = math.ceil(duration_minutes)
                
                if duration_minutes < FREE_VIDEO_DURATION_MINUTES:
                    visual_indexing_cost = 0
                    audio_indexing_cost = 0
                    storage_cost = 0
                else:
                    # Calculate costs based on Twelve Labs pricing
                    visual_indexing_cost = math.ceil(duration_minutes_ceil * TWELVE_LABS_VIDEO_INDEXING_COST)
                    audio_indexing_cost = math.ceil(duration_minutes_ceil * TWELVE_LABS_AUDIO_INDEXING_COST)
                    storage_cost = math.ceil(duration_minutes_ceil * TWELVE_LABS_STORAGE_COST)
                
                # Log the costs in millicents
                logger.info(f"Video duration: {duration_minutes} minutes")
                logger.info(f"Video duration ceiling: {duration_minutes_ceil} minutes")
                logger.info(f"Visual indexing cost: {visual_indexing_cost} millicents")
                logger.info(f"Audio indexing cost: {audio_indexing_cost} millicents")
                logger.info(f"Storage cost: {storage_cost} millicents")
                logger.info(f"Total cost: {visual_indexing_cost + audio_indexing_cost + storage_cost} millicents")

                costs = [
                    fp.CostItem(
                        amount_usd_milli_cents=visual_indexing_cost,
                        description="Visual indexing"
                    ),
                    fp.CostItem(
                        amount_usd_milli_cents=audio_indexing_cost,
                        description="Audio indexing"
                    ),
                    fp.CostItem(
                        amount_usd_milli_cents=storage_cost,
                        description="Monthly storage"
                    )
                ]

                # Authorize costs before downloading or processing
                try:
                    await self.authorize_cost(request, costs)
                except fp.InsufficientFundError:
                    logger.error("Insufficient funds for video processing")
                    return ("error", self.INSUFFICIENT_FUNDS_MESSAGE)

                # Load user data and create index if needed
                user_data = self._load_user_data(conversation_id)
                index_id = user_data.get("index_id")

                if index_id is None:
                    models = [
                        {
                            "type": "visual",
                            "name": "pegasus1.2",
                            "options": ["visual"]
                        }
                    ]
                    index_name = "poe_index_" + str(int(time.time()))
                    try:
                        index = self.client.index.create(
                            name=index_name,
                            models=models
                        )
                        index_id = index.id
                        user_data["index_id"] = index_id
                        self._save_user_data(conversation_id, user_data)
                    except twelvelabs.exceptions.TwelveLabsError as e:
                        logger.error(f"Failed to create index: {e}")
                        raise

                # Now proceed with downloading and processing
                processed_path = await self.upscale_video(downloaded_path)
                logger.info(f"Video processed and upscaled to: {processed_path}")
                
                try:
                    # Create task with processed video
                    with open(processed_path, 'rb') as video_file:
                        task = self.client.task.create(
                            index_id=index_id,
                            file=video_file
                        )
                    
                    logger.info(f"Created task: id={task.id} status={task.status}")
                    user_data["processing_task"] = task.id
                    user_data["processing_url"] = self.video_url
                    
                    # Store both the hash and video_id mapping
                    user_data["processed_videos"][video_key] = task.video_id
                    self._save_user_data(conversation_id, user_data)
                    
                    # Capture the actual cost after successful processing
                    await self.capture_cost(request, costs)
                    
                    return task.id
                    
                finally:
                    # Clean up processed video
                    if processed_path and os.path.exists(processed_path):
                        try:
                            os.unlink(processed_path)
                            logger.debug(f"Cleaned up processed video: {processed_path}")
                        except Exception as e:
                            logger.error(f"Failed to clean up processed video: {e}")

            except VideoDurationError as e:
                if str(e) == 'video_duration_too_short':
                    return ("error", self.VIDEO_TOO_SHORT_MESSAGE)
                elif str(e) == 'video_duration_too_long':
                    return ("error", self.VIDEO_TOO_LONG_MESSAGE)
                raise
            except Exception as e:
                logger.error(f"Error during video processing: {e}")
                raise

        finally:
            # Clean up downloaded video in all cases
            if downloaded_path and os.path.exists(downloaded_path):
                try:
                    os.unlink(downloaded_path)
                    logger.debug(f"Cleaned up downloaded video: {downloaded_path}")
                except Exception as e:
                    logger.error(f"Failed to clean up downloaded video: {e}")

    async def wait_for_processing(self, conversation_id, request):
        logger.info(f"Waiting for processing to complete for conversation: {conversation_id}")
        user_data = self._load_user_data(conversation_id)
        task_id = user_data.get("processing_task")
        
        if not task_id:
            # Check if we have a video_id (meaning we're reusing a processed video)
            if user_data.get("video_id"):
                # Only process pending questions if this is a reused video, not a failed upload
                if user_data.get("processing_task") is None:  # Explicitly None means reused video
                    # Check for pending question
                    pending_question = user_data.get("pending_question")
                    if pending_question:
                        logger.info(f"Found pending question for existing video: {pending_question}")
                        # Clear pending question first in case of insufficient funds
                        user_data["pending_question"] = None
                        self._save_user_data(conversation_id, user_data)
                        
                        # Calculate and authorize input costs first
                        input_tokens = len(pending_question.split())
                        input_cost = math.ceil(input_tokens * TWELVE_LABS_INPUT_TOKEN_COST)
                        
                        logger.info(f"Input tokens: {input_tokens}")
                        logger.info(f"Input cost: {input_cost} millicents")
                        
                        try:
                            # Authorize input cost first
                            await self.authorize_cost(
                                request,
                                [
                                    fp.CostItem(
                                        amount_usd_milli_cents=input_cost,
                                        description="Text generation input"
                                    ),
                                    fp.CostItem(
                                        amount_usd_milli_cents=math.ceil(OUTPUT_TEXT_TOKEN_ASSUMPTION * TWELVE_LABS_OUTPUT_TOKEN_COST),
                                        description="Text generation output"
                                    )
                                ]
                            )
                            
                            # Process the pending question
                            res = self.client.generate.text(
                                video_id=user_data["video_id"],
                                prompt=pending_question
                            )
                            response_text = self.PENDING_QUESTION_RESPONSE.format(
                                question=pending_question,
                                answer=res.data
                            )
                            
                            # Calculate and capture both input and output costs
                            output_tokens = len(res.data.split())
                            output_cost = math.ceil(output_tokens * TWELVE_LABS_OUTPUT_TOKEN_COST)
                            
                            logger.info(f"Output tokens: {output_tokens}")
                            logger.info(f"Output cost: {output_cost} millicents")
                            logger.info(f"Total text generation cost: {input_cost + output_cost} millicents")
                            
                            costs = [
                                fp.CostItem(
                                    amount_usd_milli_cents=input_cost,
                                    description="Text generation input"
                                ),
                                fp.CostItem(
                                    amount_usd_milli_cents=output_cost,
                                    description="Text generation output"
                                )
                            ]
                            
                            await self.capture_cost(request, costs)
                            yield fp.PartialResponse(text=response_text)
                            return
                            
                        except fp.InsufficientFundError:
                            yield fp.PartialResponse(text=self.INSUFFICIENT_FUNDS_MESSAGE)
                            return
                    
                    # Show completion message if no pending question
                    yield fp.PartialResponse(text=self.PROCESSING_COMPLETE_MESSAGE)
                    return
                
            yield fp.PartialResponse(text=self.NO_TASK_MESSAGE)
            return

        # Check task status with timeout
        start_time = time.time()
        last_update = start_time
        timeout = 600 - 5  # 10 minutes timeout, 5 second buffer
        
        while True:
            current_time = time.time()
            # Send a dot every 3 seconds
            if current_time - last_update >= 3:
                yield fp.PartialResponse(text=".")
                last_update = current_time
            
            if current_time - start_time > timeout:
                logger.warning(f"Task {task_id} timed out after {timeout} seconds")
                # Save task ID to allow checking status later
                user_data["processing_task"] = task_id
                self._save_user_data(conversation_id, user_data)
                yield fp.PartialResponse(text="timeout")
                return
            
            # Retrieve latest task status
            task = self.client.task.retrieve(task_id)
            
            # Replace print statements with logging
            logger.debug(f"Task {task_id} status: {task.status}")
            
            # Check task status directly
            if task.status == "ready":
                # Save video_id to user data
                user_data["video_id"] = task.video_id
                user_data["processing_task"] = None  # Clear processing task
                self._save_user_data(conversation_id, user_data)
                
                # Check for pending question
                pending_question = user_data.get("pending_question")
                if pending_question:
                    logger.info(f"Found pending question: {pending_question}")
                    # Clear pending question
                    user_data["pending_question"] = None
                    self._save_user_data(conversation_id, user_data)
                    
                    # Calculate and authorize input costs first
                    input_tokens = len(pending_question.split())
                    input_cost = math.ceil(input_tokens * TWELVE_LABS_INPUT_TOKEN_COST)
                    
                    logger.info(f"Input tokens: {input_tokens}")
                    logger.info(f"Input cost: {input_cost} millicents")
                    
                    try:
                        # Authorize input cost first
                        await self.authorize_cost(
                            request,
                            [
                                fp.CostItem(
                                    amount_usd_milli_cents=input_cost,
                                    description="Text generation input"
                                ),
                                fp.CostItem(
                                    amount_usd_milli_cents=math.ceil(OUTPUT_TEXT_TOKEN_ASSUMPTION * TWELVE_LABS_OUTPUT_TOKEN_COST),
                                    description="Text generation output"
                                )
                            ]
                        )
                        
                        # Process the pending question
                        res = self.client.generate.text(
                            video_id=user_data["video_id"],
                            prompt=pending_question
                        )
                        response_text = self.PENDING_QUESTION_RESPONSE.format(
                            question=pending_question,
                            answer=res.data
                        )
                        
                        # Calculate and capture both input and output costs
                        output_tokens = len(res.data.split())
                        output_cost = math.ceil(output_tokens * TWELVE_LABS_OUTPUT_TOKEN_COST)
                        
                        logger.info(f"Output tokens: {output_tokens}")
                        logger.info(f"Output cost: {output_cost} millicents")
                        logger.info(f"Total text generation cost: {input_cost + output_cost} millicents")
                        
                        costs = [
                            fp.CostItem(
                                amount_usd_milli_cents=input_cost,
                                description="Text generation input"
                            ),
                            fp.CostItem(
                                amount_usd_milli_cents=output_cost,
                                description="Text generation output"
                            )
                        ]
                        
                        await self.capture_cost(request, costs)
                        yield fp.PartialResponse(text=response_text)
                        return
                        
                    except fp.InsufficientFundError:
                        yield fp.PartialResponse(text=self.INSUFFICIENT_FUNDS_MESSAGE)
                        return
                    
                # Show completion message if no pending question
                yield fp.PartialResponse(text=self.PROCESSING_COMPLETE_MESSAGE)
                return
            elif task.status == "failed":
                logger.error(f"Task {task_id} failed")
                yield fp.PartialResponse(text=self.PROCESSING_FAILED_MESSAGE)
                return
            
            await asyncio.sleep(2)

    def _get_extension_from_url(self, url):
        """Extract extension from URL or default to .mp4"""
        path = Path(url.split('?')[0])  # Remove query parameters
        return path.suffix.lower() if path.suffix else '.mp4'

    def _get_extension_from_mimetype(self, mimetype):
        """Get file extension from mimetype"""
        ext = mimetypes.guess_extension(mimetype)
        return ext if ext else '.mp4'

    async def download_video(self, url_or_attachment):
        """
        Downloads video from either a URL or attachment.
        Preserves original file extension.
        """
        if isinstance(url_or_attachment, str):  # URL
            extension = self._get_extension_from_url(url_or_attachment)
        else:  # Attachment
            extension = self._get_extension_from_mimetype(url_or_attachment.content_type)

        with NamedTemporaryFile(suffix=extension, delete=False) as temp_file:
            if isinstance(url_or_attachment, str):  # URL
                async with aiohttp.ClientSession() as session:
                    async with session.get(url_or_attachment) as response:
                        if response.status != 200:
                            raise Exception(f"Failed to download video: HTTP {response.status}")
                        temp_file.write(await response.read())
            else:  # Attachment
                response = await self.client.session.get(url_or_attachment.url)
                temp_file.write(await response.read())
            
            temp_file.flush()
            return temp_file.name

    async def upscale_video(self, input_path, target_width=854, target_height=480):
        """
        Validates video length and upscales video file to the target dimensions using FFmpeg.
        Converts to MP4 (H.264) format for maximum compatibility.
        """
        logger.info(f"Processing video from: {input_path}")
        
        # Check video duration using ffprobe
        duration_command = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            input_path
        ]
        
        try:
            duration_result = subprocess.run(
                duration_command,
                check=True,
                capture_output=True,
                text=True
            )
            duration = float(duration_result.stdout)
            
            # Validate duration
            if duration < MIN_VIDEO_DURATION:
                raise VideoDurationError('video_duration_too_short')
            if duration > MAX_VIDEO_DURATION:
                raise VideoDurationError('video_duration_too_long')
            
            # Check video resolution using ffprobe
            resolution_command = [
                'ffprobe',
                '-v', 'error',
                '-select_streams', 'v:0',
                '-show_entries', 'stream=width,height',
                '-of', 'csv=s=x:p=0',
                input_path
            ]
            
            resolution_result = subprocess.run(
                resolution_command,
                check=True,
                capture_output=True,
                text=True
            )
            width, height = map(int, resolution_result.stdout.split('x'))
            
            # Check if the video needs upscaling
            if (width >= 480 and height >= 360) or (width >= 360 and height >= 480):
                logger.info("Video resolution is sufficient, no upscaling needed.")
                return input_path  # Return the original path if no upscaling is needed
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get video properties: {e.stderr}")
            raise Exception(f"Video property check failed: {e.stderr}")
        
        with NamedTemporaryFile(suffix='.mp4', delete=False) as output_file:
            # Enhanced FFmpeg command with better compatibility
            ffmpeg_command = [
                'ffmpeg',
                '-ss', str(start_time),  # Move -ss before -i for faster seeking
                '-i', video_url,
                '-t', str(end_time - start_time),
                '-c:v', 'libx264',
                '-preset', 'ultrafast',  # Fastest encoding preset
                '-tune', 'fastdecode',   # Optimize for fast decoding
                '-profile:v', 'baseline', # Simpler profile for faster encoding
                '-level', '3.0',         # Compatible level for mobile/web
                '-movflags', '+faststart', # Optimize for web playback
                '-c:a', 'aac',
                '-b:a', '128k',          # Reasonable audio quality
                '-y',
                output_file.name
            ]
            
            try:
                result = subprocess.run(
                    ffmpeg_command,
                    check=True,
                    capture_output=True,
                    text=True
                )
                logger.info(f"Video processed successfully")
                return output_file.name
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to process video: {e.stderr}")
                raise Exception(f"Video processing failed: {e.stderr}")

    async def handle_text_generation_costs(self, request, input_text, output_text):
        """
        Calculate, log, authorize and capture costs for text generation.
        Free for videos under FREE_VIDEO_DURATION_MINUTES.
        """
        # Load user data to check video duration
        user_data = self._load_user_data(request.conversation_id)
        video_url = user_data.get("processing_url")
        
        logger.info(f"Processing URL from user_data: {video_url}")
        
        if video_url:
            # Get video duration
            duration_command = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                video_url
            ]
            
            duration_result = subprocess.run(
                duration_command,
                check=True,
                capture_output=True,
                text=True
            )
            duration_minutes = float(duration_result.stdout) / 60
            
            # If video is under FREE_VIDEO_DURATION_MINUTES, make it free
            if duration_minutes < FREE_VIDEO_DURATION_MINUTES:
                logger.info(f"Video duration ({duration_minutes:.2f} minutes) is under {FREE_VIDEO_DURATION_MINUTES} minute - no charge")
                return True, None
        else:
            logger.warning(f"No processing_url found in user_data: {user_data}")

        # Regular cost calculation for longer videos
        input_tokens = len(input_text.split())
        input_cost = math.ceil(input_tokens * TWELVE_LABS_INPUT_TOKEN_COST)

        logger.info(f"Input tokens: {input_tokens}")
        logger.info(f"Input cost: {input_cost} millicents")

        try:
            await self.authorize_cost(
                request,
                [
                    fp.CostItem(
                        amount_usd_milli_cents=input_cost,
                        description="Text generation input"
                    ),
                    fp.CostItem(
                        amount_usd_milli_cents=math.ceil(OUTPUT_TEXT_TOKEN_ASSUMPTION * TWELVE_LABS_OUTPUT_TOKEN_COST),
                        description="Text generation output"
                    )
                ]
            )
            
            output_tokens = len(output_text.split())
            output_cost = math.ceil(output_tokens * TWELVE_LABS_OUTPUT_TOKEN_COST)

            logger.info(f"Output tokens: {output_tokens}")
            logger.info(f"Output cost: {output_cost} millicents")
            logger.info(f"Total text generation cost: {input_cost + output_cost} millicents")

            costs = [
                fp.CostItem(
                    amount_usd_milli_cents=input_cost,
                    description="Text generation input"
                ),
                fp.CostItem(
                    amount_usd_milli_cents=output_cost,
                    description="Text generation output"
                )
            ]

            await self.capture_cost(request, costs)
            return True, None

        except fp.InsufficientFundError:
            error_response = fp.ErrorResponse(
                text=self.INSUFFICIENT_FUNDS_MESSAGE,
                error_type="insufficient_fund"
            )
            return False, error_response

    async def extract_video_segment(self, video_url, start_time, end_time):
        """
        Extracts a segment of video between start_time and end_time.
        Returns the path to the extracted segment.
        """
        try:
            start = time.time()
            # Create a temporary file for the segment
            with NamedTemporaryFile(suffix='.mp4', delete=False) as output_file:
                ffmpeg_command = [
                    'ffmpeg',
                    '-ss', str(start_time),  # Move -ss before -i for faster seeking
                    '-i', video_url,
                    '-t', str(end_time - start_time),
                    '-c:v', 'libx264',
                    '-preset', 'ultrafast',  # Fastest encoding preset
                    '-tune', 'fastdecode',   # Optimize for fast decoding
                    '-profile:v', 'baseline', # Simpler profile for faster encoding
                    '-level', '3.0',         # Compatible level for mobile/web
                    '-movflags', '+faststart', # Optimize for web playback
                    '-c:a', 'aac',
                    '-b:a', '128k',          # Reasonable audio quality
                    '-y',
                    output_file.name
                ]
                
                result = subprocess.run(
                    ffmpeg_command,
                    check=True,
                    capture_output=True,
                    text=True
                )
                duration = time.time() - start
                logger.info(f"Video segment extracted successfully in {duration:.2f} seconds")
                return output_file.name
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to extract video segment: {e.stderr}")
            return None

# Create shared volume for persistent storage
storage_vol = Volume.from_name("twelve-labs-storage", create_if_missing=True)


#deploy the app
REQUIREMENTS = ["fastapi-poe==0.0.53","twelvelabs==0.4.3"]
image = (
    Image.debian_slim()
    .apt_install([
        "ffmpeg",
        "libx264-dev",
        "libavcodec-extra",
        "libavformat-dev",
        "libavutil-dev",
        "libswscale-dev"
    ])
    .pip_install(*REQUIREMENTS)
    # .pip_install("twelvelabs==0.4.3") #force install
)
app = App("twelve-labs-bot-poe")


@app.function(
    image=image,
    timeout=86400,  # 24 hours in seconds
    container_idle_timeout=1200,
    volumes={"/storage": storage_vol},  # Mount the volume at /storage
    secrets=[
        Secret.from_name("POE_BOT_ACCESS_KEY"),
        Secret.from_name("POE_BOT_NAME"),
        Secret.from_name("TWELVE_LABS_API_KEY"),
        Secret.from_name("TEST_MODE")  # Add TEST_MODE secret
    ]
)
@asgi_app()
def fastapi_app():
    logger.info("Initializing FastAPI application")
    ACCESS_KEY = os.environ["POE_BOT_ACCESS_KEY"]
    BOT_NAME = os.environ["POE_BOT_NAME"]
    TWELVE_LABS_API_KEY = os.environ["TWELVE_LABS_API_KEY"]
    
    # Pass the storage_dir parameter
    bot = TwelveLabsBot(api_key=TWELVE_LABS_API_KEY, storage_dir="/storage")
    app = fp.make_app(bot, access_key=ACCESS_KEY, bot_name=BOT_NAME)
    return app

# Create a FastAPI app for serving videos
video_app = FastAPI()

@app.function(
    image=image,
    volumes={"/storage": storage_vol}
)
@web_endpoint(method="GET")
async def serve_video_segment(segment_id: str):
    """Serve a video segment from storage"""
    video_path = Path("/storage/segments") / f"{segment_id}.mp4"
    if not video_path.exists():
        return {"error": "Video segment not found"}, 404
    
    return FileResponse(video_path, media_type="video/mp4")