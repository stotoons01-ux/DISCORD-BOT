"""
Advanced OpenRouter API Key Manager with Robust Failover System

This module provides a comprehensive solution for managing multiple OpenRouter API keys
with intelligent rotation, error handling, rate limit management, and caching.

Features:
- Automatic API key rotation on failures
- Circuit breaker pattern to avoid repeated failures
- Request caching to reduce API calls
- Alliance data formatting and multi-message support
- Rate limit tracking and prediction
- Exponential backoff retry logic
- Comprehensive logging and monitoring
- Async-first design for better performance
"""

import asyncio
import aiohttp
import logging
import time
import json
import hashlib
import os
from typing import List, Dict, Optional, Any, Union
from dataclasses import dataclass, field
from enum import Enum
from collections import defaultdict, deque
import random
from dotenv import load_dotenv
from sheets_manager import SheetsManager, is_event_related_query
from alliance_filter import filter_sheet_data, format_alliance_data, is_alliance_related

# Load environment variables
load_dotenv()
load_dotenv('.env.production')

# Configure logging
logger = logging.getLogger(__name__)


class APIKeyStatus(Enum):
    """Enum for API key status"""
    HEALTHY = "healthy"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"
    CIRCUIT_OPEN = "circuit_open"

@dataclass
class APIKeyInfo:
    """Information about an API key's performance and status"""
    key: str
    index: int
    status: APIKeyStatus = APIKeyStatus.HEALTHY
    last_success: float = field(default_factory=time.time)
    last_failure: float = 0.0
    consecutive_failures: int = 0
    total_requests: int = 0
    successful_requests: int = 0
    rate_limit_reset_time: float = 0.0
    circuit_breaker_open_until: float = 0.0
    response_times: deque = field(default_factory=lambda: deque(maxlen=10))

    @property
    def success_rate(self) -> float:
        """Calculate success rate"""
        if self.total_requests == 0:
            return 1.0
        return self.successful_requests / self.total_requests

    @property
    def average_response_time(self) -> float:
        """Calculate average response time"""
        if not self.response_times:
            return 0.0
        return sum(self.response_times) / len(self.response_times)

    @property
    def is_healthy(self) -> bool:
        """Check if the API key is healthy and available"""
        current_time = time.time()

        # Check if circuit breaker is open
        if self.circuit_breaker_open_until > current_time:
            return False

        # Check if rate limited
        if self.rate_limit_reset_time > current_time:
            return False

        # Check consecutive failures
        if self.consecutive_failures >= 3:
            return False

        return True

class RobustOpenRouterManager:
    """Robust manager for OpenRouter API requests with key rotation and failover"""

    def __init__(self, api_keys: List[str], model: Optional[str] = None):
        self.api_keys = [APIKeyInfo(key=key, index=i) for i, key in enumerate(api_keys)]
        self.model = model
        self.sheets_manager = SheetsManager()
        self.spreadsheet_id = os.getenv('GOOGLE_SHEET_ID')
        self.cache: Dict[str, str] = {}
        self._lock = asyncio.Lock()
        self.cache_hits = 0
        self.total_requests = 0
        self.max_retries = 3
        self.base_backoff = 1.0

        # Log key loading
        logger.info(f"Loaded {len(api_keys)} API keys for OpenRouter. Model: {model}")
        if not api_keys:
            logger.warning("No API keys loaded. API calls will return placeholders.")

    async def make_request(self, messages: List[Dict[str, str]], max_tokens: int = 1000) -> str:
        """Make an async request to OpenRouter API with key rotation and caching"""
        if not self.api_keys:
            return "Placeholder: No API keys configured. Please set OPENROUTER_API_KEY_1 in .env for real responses."

        # Check cache first
        cache_key = hashlib.md5(json.dumps(messages, sort_keys=True).encode()).hexdigest()
        async with self._lock:
            if cache_key in self.cache:
                self.cache_hits += 1
                logger.info("Cache hit for API request")
                return self.cache[cache_key]

        start_time = time.time()
        self.total_requests += 1

        # Try keys with rotation and failover
        keys_to_try = self.api_keys[:]  # Copy list
        random.shuffle(keys_to_try)  # Randomize order for better distribution

        for attempt in range(self.max_retries):
            any_healthy = any(k.is_healthy for k in keys_to_try)
            if not any_healthy:
                logger.error("No healthy API keys available to try.")
                break

            for key_info in keys_to_try:
                if not key_info.is_healthy:
                    continue

                try:
                    response = await self._request_with_key(key_info, messages, max_tokens)

                    # Update success stats
                    key_info.total_requests += 1
                    key_info.successful_requests += 1
                    key_info.consecutive_failures = 0
                    key_info.last_success = time.time()
                    key_info.response_times.append(time.time() - start_time)

                    # Cache the response
                    async with self._lock:
                        self.cache[cache_key] = response
                        logger.info(f"API request successful with key {key_info.index + 1}. Cached response.")

                    return response

                except aiohttp.ClientError as e:
                    err_text = str(e).lower()
                    # If it's an insufficient credits error, we've already marked the key as failed in _request_with_key.
                    if 'insufficient credits' in err_text or 'insufficient' in err_text:
                        logger.error(f"Key {key_info.index + 1} disabled due to insufficient credits: {e}")
                        # don't backoff: move to next key immediately
                        continue

                    # Generic failure: update key status and backoff
                    self._update_key_status(key_info, False, str(e))
                    logger.warning(f"API request failed with key {key_info.index + 1}: {e}")

                    # Exponential backoff
                    backoff_time = self.base_backoff * (2 ** attempt)
                    await asyncio.sleep(backoff_time)

        # All attempts failed
        error_msg = f"All API requests failed after {self.max_retries} attempts"
        logger.error(error_msg)
        raise Exception(error_msg)

    async def _request_with_key(self, key_info: APIKeyInfo, messages: List[Dict[str, str]], max_tokens: int) -> str:
        """Make a single request with a specific key"""
        url = "https://openrouter.ai/api/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens
        }

        headers = {
            "Authorization": f"Bearer {key_info.key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/your-repo/angel-bot",  # Replace with your actual domain
            "X-Title": "Angel Bot"
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["choices"][0]["message"]["content"]
                else:
                    # Read error body for diagnostics
                    error_text = await response.text()
                    # If the account has insufficient credits (HTTP 402), open the circuit breaker for this key longer
                    if response.status == 402:
                        logger.error(f"API key {key_info.index + 1} returned 402 Insufficient credits: {error_text}")
                        # If the configured model is a free model (contains ':free') or the env var
                        # OPENROUTER_402_TREAT_AS_TRANSIENT is set to 'true', treat 402 as transient and
                        # avoid opening a long circuit breaker. Some free models may return 402 in
                        # specific conditions but the key may still be usable shortly.
                        treat_as_transient = False
                        try:
                            env_flag = os.getenv('OPENROUTER_402_TREAT_AS_TRANSIENT', '').lower()
                            if env_flag in ('1', 'true', 'yes'):
                                treat_as_transient = True
                        except Exception:
                            pass

                        if (self.model and ':free' in str(self.model)) or treat_as_transient:
                            logger.warning(f"Treating 402 as transient for key {key_info.index + 1} (model={self.model})")
                            # mark as rate limited for a short window instead of failed long-term
                            key_info.status = APIKeyStatus.RATE_LIMITED
                            key_info.rate_limit_reset_time = time.time() + 60  # 1 minute pause
                            key_info.consecutive_failures += 1
                            key_info.last_failure = time.time()
                            raise aiohttp.ClientError(f"Transient insufficient credits (treated as rate-limited) for API key {key_info.index + 1}: {error_text}")
                        else:
                            # Mark key as failed and open circuit for 24 hours to avoid retry storms
                            key_info.status = APIKeyStatus.FAILED
                            key_info.circuit_breaker_open_until = time.time() + 24 * 3600
                            key_info.consecutive_failures += 1
                            key_info.last_failure = time.time()
                            raise aiohttp.ClientError(f"Insufficient credits for API key {key_info.index + 1}: {error_text}")
                    else:
                        raise aiohttp.ClientError(f"API request failed with status {response.status}: {error_text}")

    def _update_key_status(self, key_info: APIKeyInfo, success: bool, error_msg: str = ""):
        """Update the status of an API key based on request results"""
        if success:
            key_info.status = APIKeyStatus.HEALTHY
            key_info.consecutive_failures = 0
        else:
            key_info.consecutive_failures += 1
            key_info.last_failure = time.time()

            if key_info.consecutive_failures >= 3:
                key_info.status = APIKeyStatus.FAILED
                key_info.circuit_breaker_open_until = time.time() + 300  # 5 minutes circuit breaker
            elif "rate limit" in error_msg.lower():
                key_info.status = APIKeyStatus.RATE_LIMITED
                # Try to extract reset time from error message
                key_info.rate_limit_reset_time = time.time() + 60  # Default 1 minute

    async def get_stats(self) -> Dict[str, Any]:
        """Get statistics about API usage and key performance"""
        return {
            "total_requests": self.total_requests,
            "cache_hits": self.cache_hits,
            "cache_hit_rate": self.cache_hits / max(self.total_requests, 1),
            "api_keys": [
                {
                    "index": key_info.index,
                    "status": key_info.status.value,
                    "success_rate": key_info.success_rate,
                    "total_requests": key_info.total_requests,
                    "successful_requests": key_info.successful_requests,
                    "average_response_time": key_info.average_response_time,
                    "consecutive_failures": key_info.consecutive_failures
                }
                for key_info in self.api_keys
            ]
        }


# Global manager instance
def get_api_keys() -> List[str]:
    """Get API keys from environment variables"""
    keys = []
    for i in range(1, 10):  # Support up to 9 keys
        key = os.getenv(f'OPENROUTER_API_KEY_{i}')
        if key:
            keys.append(key)
        else:
            break
    return keys


# Create global manager instance
api_keys = get_api_keys()

# Determine model from environment (allow override via OPENROUTER_MODEL)
env_model = (os.getenv('OPENROUTER_MODEL') or '').strip()
fallback = (os.getenv('OPENROUTER_FREE_FALLBACK') or '').strip()

# Reject paid OpenAI-style models entirely — only allow free models set in .env
if not env_model:
    if fallback:
        chosen_model = fallback
        logger.warning("OPENROUTER_MODEL not set — falling back to OPENROUTER_FREE_FALLBACK")
    else:
        logger.error("OPENROUTER_MODEL is not set and no OPENROUTER_FREE_FALLBACK provided. Please set a free model in .env (e.g. 'model:free').")
        raise RuntimeError("OPENROUTER_MODEL is required and must be a free model")
else:
    # If the env_model looks like a paid OpenAI model, reject it and require a free model
    lower = env_model.lower()
    if ('openai' in lower or 'gpt-' in lower) and ':free' not in lower:
        if fallback:
            chosen_model = fallback
            logger.warning(f"OPENROUTER_MODEL '{env_model}' appears to be a paid model — falling back to free model '{fallback}' as paid models are disabled.")
        else:
            logger.error(f"OPENROUTER_MODEL '{env_model}' appears to be a paid model. Paid models are disabled for this deployment. Set OPENROUTER_MODEL to a free model in .env.")
            raise RuntimeError("Paid OpenRouter models are disabled. Set OPENROUTER_MODEL to a free model.")
    else:
        chosen_model = env_model

manager = RobustOpenRouterManager(api_keys, model=chosen_model)
logger.info(f"OpenRouter model in use: {manager.model}")


async def make_request(messages: List[Dict[str, str]], max_tokens: int = 1000, include_sheet_data: bool = True) -> str:
    """
    Global function to make API requests using the manager
    
    Args:
        messages: List of message dictionaries for the chat
        max_tokens: Maximum tokens in response
        include_sheet_data: Whether to include alliance and event data in system message
    """
    # Check if we should include sheet data
    if include_sheet_data and messages and messages[0]['role'] == 'system':
        # Get user's question (last message in the conversation)
        user_question = messages[-1]['content'] if messages[-1]['role'] == 'user' else ''
        
        if not manager.spreadsheet_id:
            logger.error("GOOGLE_SHEET_ID is not set in .env file")
            return await manager.make_request(messages, max_tokens)
        
        try:
            # Determine what type of data to include
            is_event = is_event_related_query(user_question)
            is_alliance = is_alliance_related(user_question)
            sheet_data = ""
            
            if is_event:
                logger.info("Attempting to fetch event guide data from Google Sheets...")
                event_data = await manager.sheets_manager.get_event_guides(manager.spreadsheet_id)
                if event_data:
                    event_text = manager.sheets_manager.format_event_guides_for_prompt(event_data)
                    sheet_data += "\n\nEvent Guide Data:\n" + event_text
                else:
                    logger.warning("No event guide data retrieved from sheet")
            
            if is_alliance:
                logger.info("Attempting to fetch alliance data from Google Sheets...")
                alliance_data = await manager.sheets_manager.get_alliance_data(manager.spreadsheet_id)
                if alliance_data:
                    # Check if this is a request about ICE members
                    if 'ice' in user_question.lower() and ('list' in user_question.lower() or 'all' in user_question.lower()):
                        # Filter data for ICE alliance
                        filtered_data = [x for x in alliance_data if x.get('Alliance Name', '').upper() == 'ICE']
                        if filtered_data:
                            formatted_messages = format_alliance_data(filtered_data, user_question + " with power")  # Added "with power" to force power display
                            return "ALLIANCE_MESSAGES:" + json.dumps(formatted_messages)
                    alliance_text = manager.sheets_manager.format_alliance_data_for_prompt(alliance_data)
                    sheet_data += "\n\nCurrent Alliance Data:\n" + alliance_text
                else:
                    logger.warning("No alliance data retrieved from sheet")
                    
            if not sheet_data:
                return await manager.make_request(messages, max_tokens)
            
            # Filter and format all collected data
            system_msg = messages[0]
            system_msg['content'] = f"{system_msg['content']}{sheet_data}"
            messages[0] = system_msg
            logger.info("Injected sheet data into prompt")
            return await manager.make_request(messages, max_tokens)
                
        except Exception as e:
            logger.error(f"Failed to inject sheet data: {str(e)}", exc_info=True)
            return await manager.make_request(messages, max_tokens)


async def make_image_request(prompt: str, api_key: str = None, width: int = None, height: int = None, model: str = None) -> bytes:
    """Make a request to generate an image using Hugging Face or OpenAI API.

    Args:
        prompt: Text prompt to generate the image from.
        api_key: Optional API key override (not used currently).
        width: Optional desired image width in pixels.
        height: Optional desired image height in pixels.
        model: Optional model identifier to override the HUGGINGFACE_MODEL env var.

    Returns:
        Raw image bytes.
    """

    # First try Hugging Face
    # Collect all HUGGINGFACE_API_TOKEN* env vars dynamically (preserve insertion order)
    hf_tokens = []
    for k, v in os.environ.items():
        if k.startswith('HUGGINGFACE_API_TOKEN') and v:
            hf_tokens.append(v)

    if hf_tokens:
        logger.info("Trying Hugging Face API for image generation")
        # Allow the caller to override the model; otherwise fall back to env
        hf_model = model or os.getenv('HUGGINGFACE_MODEL', 'stabilityai/stable-diffusion-xl-base-1.0')
        # Use the new Hugging Face Inference Router endpoint (router base)
        # The router expects the model name in the JSON payload rather than the URL path.
        url = "https://router.huggingface.co/hf-inference"

        # Determine default sizes based on model family
        is_xl = "xl" in hf_model.lower()
        default_w = 1024 if is_xl else 512
        default_h = 1024 if is_xl else 512

        # Use provided width/height if available, otherwise defaults
        use_w = int(width) if width else default_w
        use_h = int(height) if height else default_h

        # Adjust parameters based on model
        common_parameters = {
            "negative_prompt": "blurry, low quality, distorted",
            "guidance_scale": 7.5,
            "width": use_w,
            "height": use_h,
        }

        if is_xl:
            # SDXL parameters
            common_parameters["num_inference_steps"] = 30
        else:
            # SD 1.5 or other models - smaller, faster
            common_parameters["num_inference_steps"] = 20

        # Router expects top-level model key plus inputs and parameters
        payload = {
            "model": hf_model,
            "inputs": prompt,
            "parameters": common_parameters,
        }

        # Try each token
        local_invalid_tokens = set()
        for token in hf_tokens:
            if token in local_invalid_tokens:
                continue
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            try:
                async with aiohttp.ClientSession() as session:
                    # Try two router patterns: with model in the path, and with model in payload
                    url_with_model = f"https://router.huggingface.co/hf-inference/{hf_model}"
                    url_base = url  # https://router.huggingface.co/hf-inference

                    # Prepare two payloads: one without 'model' for the path-style, one with 'model' for the base router
                    payload_path = {"inputs": prompt, "parameters": common_parameters}
                    payload_base = payload

                    for attempt_url, attempt_payload in ((url_with_model, payload_path), (url_base, payload_base)):
                        try:
                            async with session.post(attempt_url, json=attempt_payload, headers=headers, timeout=120) as response:
                                text_ct = response.headers.get("Content-Type", "") or response.headers.get("content-type", "")
                                body_text = await response.text()

                                # If we got a 403/401, inspect body for expired token or permission issues
                                if response.status in (401, 403):
                                    logger.warning(f"Hugging Face unauthorized for token ending in ...{token[-4:]}: {body_text}")
                                    # Try to parse JSON error messages for 'expired' or similar hints
                                    try:
                                        err = json.loads(body_text)
                                        err_msg = err.get("error") if isinstance(err, dict) else str(err)
                                    except Exception:
                                        err_msg = body_text

                                    if isinstance(err_msg, str) and ("expired" in err_msg.lower() or "expired" in body_text.lower()):
                                        logger.warning(f"Hugging Face token ending in ...{token[-4:]} appears expired. Please refresh the token in your .env")
                                        local_invalid_tokens.add(token)
                                        # don't retry this token for other router urls
                                        continue
                                    # otherwise, try next URL/pattern
                                    continue

                                # If we got a 404, the model may not exist or your account lacks access
                                if response.status == 404:
                                    logger.warning(f"Hugging Face router {attempt_url} returned 404 (model not found or no access): {body_text}")
                                    # try next URL or next token
                                    continue

                                # If we get raw image bytes
                                if response.status == 200 and text_ct.startswith("image/"):
                                    logger.info("Successfully generated image with Hugging Face (binary)")
                                    return await response.read()

                                # If JSON returned, try to parse and look for base64 image fields
                                if response.status == 200:
                                    try:
                                        j = json.loads(body_text)
                                        # Common patterns: {'images': ['data:image/png;base64,...']} or {'image': 'data:...base64,...'}
                                        # Or 'data' field with base64
                                        b64_str = None
                                        if isinstance(j, dict):
                                            for key in ("images", "image", "data", "result"):
                                                if key in j:
                                                    val = j[key]
                                                    if isinstance(val, list) and val:
                                                        candidate = val[0]
                                                    else:
                                                        candidate = val
                                                    if isinstance(candidate, str) and "base64" in candidate:
                                                        # Strip data:image/...;base64, prefix if present
                                                        if "," in candidate:
                                                            b64_str = candidate.split(",", 1)[1]
                                                        else:
                                                            b64_str = candidate
                                                        break
                                        if b64_str:
                                            import base64

                                            try:
                                                data_bytes = base64.b64decode(b64_str)
                                                logger.info("Successfully decoded base64 image from Hugging Face JSON response")
                                                return data_bytes
                                            except Exception as be:
                                                logger.warning(f"Failed to decode base64 from HF JSON: {be}")
                                        # If JSON contains an 'error' field, treat as failure and continue
                                        if isinstance(j, dict) and j.get("error"):
                                            logger.warning(f"Hugging Face returned error JSON: {j.get('error')}")
                                            continue
                                    except Exception:
                                        # Non-JSON or unhandled JSON structure -> fallthrough to logging
                                        pass

                                # Handle known non-200 statuses
                                if response.status == 503:
                                    # Model loading, continue to next token
                                    logger.warning(f"Hugging Face model loading for token ending in ...{token[-4:]}: {body_text}")
                                    break
                                if response.status == 401:
                                    # Unauthorized, try next token
                                    logger.warning(f"Hugging Face unauthorized for token ending in ...{token[-4:]}: {body_text}")
                                    break

                                # Log whatever we received and try next token
                                logger.warning(f"Hugging Face failed with token ending in ...{token[-4:]}: status {response.status}: {body_text}")
                                break
                        except Exception as e:
                            logger.warning(f"Hugging Face request exception for url {attempt_url} with token ending in ...{token[-4:]}: {e}")
                            # Try next URL for the same token
                            continue
            except Exception as e:
                logger.warning(f"Hugging Face exception with token ending in ...{token[-4:]}: {e}")
                continue

    # If Hugging Face failed or no tokens, try OpenAI DALL-E
    openai_key = os.getenv('OPENAI_API_KEY')
    if openai_key:
        logger.info("Trying OpenAI DALL-E API for image generation")
        url = "https://api.openai.com/v1/images/generations"
        payload = {
            "prompt": prompt,
            "n": 1,
            "size": "512x512"
        }
        headers = {
            "Authorization": f"Bearer {openai_key}",
            "Content-Type": "application/json"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=120) as response:
                    if response.status == 200:
                        data = await response.json()
                        image_url = data["data"][0]["url"]
                        # Download the image
                        async with session.get(image_url) as img_response:
                            if img_response.status == 200:
                                logger.info("Successfully generated image with OpenAI DALL-E")
                                return await img_response.read()
                            else:
                                logger.warning(f"Failed to download image from OpenAI: {img_response.status}")
                    else:
                        error_text = await response.text()
                        logger.warning(f"OpenAI DALL-E failed: status {response.status}: {error_text}")
        except Exception as e:
            logger.warning(f"OpenAI DALL-E exception: {e}")

    # If all failed
    raise Exception("Image generation failed: All APIs exhausted or unavailable")
