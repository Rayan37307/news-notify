#!/usr/bin/env python3
"""
Bangladesh Guardian News Bot - Sends news as image cards to Telegram
Author: AI Assistant
Date: September 2025
"""

import logging
import os
import requests
from requests.exceptions import RequestException
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
import time
import signal
import sys
import json
import asyncio
import re
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
import tempfile
import shutil
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ===== CONFIGURATION =====
NEWS_URL = "https://www.bangladeshguardian.com/latest"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8284162964:AAFoSVtKcaw7_NBv5wIYtWL7gNtBzUHlkKY")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1002969172101")
DEBUG_CHAT_ID = os.getenv("DEBUG_CHAT_ID", "7643719042")
CHECK_INTERVAL = 300  # 5 minutes
POSTED_LINKS_FILE = "posted_links.json"
LOG_FILE = "news_bot.log"

# ===== LOGGING SETUP =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ],
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)

# ===== GLOBAL VARIABLES =====
posted_links = set()

# ===== UTILITY FUNCTIONS =====
def sanitize_text(text):
    """Mask sensitive words before rendering or sending messages.
    Applies case-insensitive exact-word masking per provided list.
    """
    if not text:
        return text
    replacements = {
        # Kill family
        r"\bkill\b": "Ki*ll",
        r"\bkills\b": "Ki*lls",
        r"\bkilled\b": "Kil*led",
        # Murder family
        r"\bmurder\b": "Mu*rder",
        r"\bmurders\b": "Mu*rders",
        r"\bmurdered\b": "Mur*dered",
        # Assassinate family
        r"\bassassinate\b": "As*sa*ssinate",
        r"\bassassinates\b": "As*sa*ssinates",
        r"\bassassinated\b": "As*sa*ssinated",
        # Stab family
        r"\bstab\b": "St*ab",
        r"\bstabs\b": "St*abs",
        r"\bstabbed\b": "St*abbed",
        # Slaughter family
        r"\bslaughter\b": "Sl*aughter",
        r"\bslaughters\b": "Sl*aughters",
        r"\bslaughtered\b": "Sl*aughtered",
        # Rape family
        r"\brape\b": "Ra*pe",
        r"\brapes\b": "Ra*pes",
        r"\braped\b": "Ra*ped",
        # Geo/political
        r"\bgaza\b": "Ga*za",
        r"\bisrael\b": "Isr*ael",
        r"\bpalestine\b": "Pa*le*stine",
    }
    sanitized = text
    for pattern, replacement in replacements.items():
        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
    return sanitized
def load_posted_links():
    """Load previously posted links from file"""
    global posted_links
    try:
        with open(POSTED_LINKS_FILE, "r") as f:
            posted_links = set(json.load(f))
        logger.info(f"Loaded {len(posted_links)} previously posted links")
    except FileNotFoundError:
        posted_links = set()
        logger.info("No previous links found, starting fresh")

def save_posted_links():
    """Save posted links to file"""
    try:
        with open(POSTED_LINKS_FILE, "w") as f:
            json.dump(list(posted_links), f, indent=2)
        logger.info(f"Saved {len(posted_links)} posted links")
    except Exception as e:
        logger.error(f"Failed to save posted links: {e}")

def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    logger.info("Shutting down bot...")
    save_posted_links()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ===== IMAGE CREATION FUNCTIONS =====
def fetch_article_image(article_url):
    """Fetch the best main image from an article's details page.
    Preference order: og:image/twitter:image > JSON-LD image > in-article img (non-logo, sufficiently large).
    If a clickable image links to an image/detail page, follow it once and retry extraction.
    """
    def make_absolute(base_url, maybe_url):
        try:
            if not maybe_url:
                return None
            from urllib.parse import urljoin
            return urljoin(base_url, maybe_url)
        except Exception:
            return maybe_url

    def is_probably_logo(url_str: str) -> bool:
        if not url_str:
            return True
        lowered = url_str.lower()
        banned_keywords = ["logo", "favicon", "sprite", "placeholder", "default", "avatar"]
        return any(k in lowered for k in banned_keywords)

    def download_and_validate_image(img_url: str):
        try:
            resp = requests.get(img_url, timeout=15, stream=True)
            if resp.status_code != 200:
                return None
            img_data = BytesIO(resp.content)
            try:
                with Image.open(img_data) as im:
                    width_px, height_px = im.size
                    if width_px < 400 or height_px < 250:
                        return None
                img_data.seek(0)
                return img_data
            except Exception:
                return None
        except Exception:
            return None

    try:
        logger.info(f"Fetching image from: {article_url}")

        options = Options()
        options.headless = True
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
        # Use system Google Chrome if available
        chrome_bin = os.getenv("CHROME_BIN")
        if chrome_bin and os.path.exists(chrome_bin):
            options.binary_location = chrome_bin

        driver = None
        tmp_profile_dir = None
        try:
            # Use a unique user-data-dir to avoid profile lock conflicts on servers
            tmp_profile_dir = tempfile.mkdtemp(prefix="newsbot-chrome-")
            options.add_argument(f"--user-data-dir={tmp_profile_dir}")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            # Use a matching chromedriver with writable cache directory
            cache_root = os.getenv("WDM_CACHE", os.path.join(os.path.dirname(__file__), ".wdm"))
            os.makedirs(cache_root, exist_ok=True)
            service = ChromeService(ChromeDriverManager(path=cache_root).install())
            # Optional extra runtime flags from env
            extra = os.getenv("CHROME_EXTRA_ARGS")
            if extra:
                for arg in extra.split():
                    options.add_argument(arg)
            driver = webdriver.Chrome(service=service, options=options)
            driver.get(article_url)

            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            soup = BeautifulSoup(driver.page_source, "html.parser")

            # 1) Try meta tags
            meta_props = [
                ("meta", {"property": "og:image"}, "content"),
                ("meta", {"name": "twitter:image"}, "content"),
                ("meta", {"property": "twitter:image"}, "content"),
                ("meta", {"itemprop": "image"}, "content"),
            ]
            for tag, attrs, attr_field in meta_props:
                el = soup.find(tag, attrs=attrs)
                if el and el.get(attr_field):
                    url_abs = make_absolute(article_url, el.get(attr_field))
                    if url_abs and not is_probably_logo(url_abs):
                        img = download_and_validate_image(url_abs)
                        if img:
                            logger.info(f"Using meta image: {url_abs}")
                            return img

            # 2) Try JSON-LD
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.text)
                except Exception:
                    continue
                candidates = []
                if isinstance(data, dict):
                    candidates.append(data)
                elif isinstance(data, list):
                    candidates.extend(data)
                for obj in candidates:
                    img_field = obj.get("image") if isinstance(obj, dict) else None
                    url_candidate = None
                    if isinstance(img_field, str):
                        url_candidate = img_field
                    elif isinstance(img_field, list) and img_field:
                        url_candidate = img_field[0]
                    elif isinstance(img_field, dict):
                        url_candidate = img_field.get("url")
                    if url_candidate:
                        url_abs = make_absolute(article_url, url_candidate)
                        if url_abs and not is_probably_logo(url_abs):
                            img = download_and_validate_image(url_abs)
                            if img:
                                logger.info(f"Using JSON-LD image: {url_abs}")
                                return img

            # 3) In-article images with filters and lazy attrs
            img_candidates = []
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or img.get("data-original") or img.get("srcset")
                if not src:
                    continue
                # If srcset, take first URL
                if "srcset" in (img.attrs.keys()):
                    srcset = img.get("srcset")
                    if srcset:
                        src = srcset.split(",")[0].split()[0]
                url_abs = make_absolute(article_url, src)
                if not url_abs or is_probably_logo(url_abs):
                    continue
                # Prefer images inside article/entry containers
                parent_classes = " ".join(img.parent.get("class", [])) if img.parent else ""
                score = 0
                if any(k in parent_classes.lower() for k in ["article", "entry", "content", "post", "news"]):
                    score += 2
                if any(k in (img.get("class") or []) for k in ["featured", "main", "hero", "lead"]):
                    score += 3
                img_candidates.append((score, url_abs, img))
            img_candidates.sort(key=lambda t: t[0], reverse=True)

            for _, url_abs, img_tag in img_candidates[:8]:
                img = download_and_validate_image(url_abs)
                if img:
                    logger.info(f"Using in-article image: {url_abs}")
                    return img
                # If image failed or small, follow parent link if exists
                parent = img_tag.parent
                if parent and parent.name == "a" and parent.get("href"):
                    href = make_absolute(article_url, parent.get("href"))
                    if href and href != article_url:
                        try:
                            driver.get(href)
                            WebDriverWait(driver, 8).until(
                                EC.presence_of_element_located((By.TAG_NAME, "body"))
                            )
                            inner_soup = BeautifulSoup(driver.page_source, "html.parser")
                            # Try meta first on image/detail page
                            el = inner_soup.find("meta", {"property": "og:image"})
                            if el and el.get("content"):
                                cand = make_absolute(href, el.get("content"))
                                if cand and not is_probably_logo(cand):
                                    img2 = download_and_validate_image(cand)
                                    if img2:
                                        logger.info(f"Using followed detail image: {cand}")
                                        return img2
                            # fallback first img
                            first_img = inner_soup.find("img")
                            if first_img:
                                cand = make_absolute(href, first_img.get("src"))
                                if cand and not is_probably_logo(cand):
                                    img2 = download_and_validate_image(cand)
                                    if img2:
                                        logger.info(f"Using followed page img: {cand}")
                                        return img2
                        except Exception:
                            pass

            logger.warning("No suitable image found after exhaustive checks")
            return None

        except Exception as e:
            logger.error(f"Error fetching image: {e}")
            return None
        finally:
            if driver:
                driver.quit()
            if tmp_profile_dir:
                shutil.rmtree(tmp_profile_dir, ignore_errors=True)

    except Exception as e:
        logger.error(f"Error in fetch_article_image: {e}")
        return None

def create_professional_news_card(title, article_url):
    """Create a news card by compositing onto template imagecard.jpg.
    - Paste article image into the white top container
    - Put date on the left of the middle bar
    - Put title bottom-aligned inside the middle bar
    """
    logger.info(f"Creating news card for: {title[:50]}...")
    # Sanitize title for rendering
    title = sanitize_text(title)
    
    try:
        # Load base template image
        template_path = os.path.join(os.path.dirname(__file__), "imagecard.jpg")
        try:
            image = Image.open(template_path).convert("RGB")
        except Exception:
            # Fallback to previous solid background if template missing
            image = Image.new("RGB", (900, 600), color=(255, 255, 255))
        # Keep template's native output size (no forced resizing)
        width, height = image.size
        draw = ImageDraw.Draw(image)
        
        # Load fonts
        try:
            title_font = None
            date_font = None
            logo_font = None
            button_font = None
            
            # Prefer Cambria Bold for heading if available
            font_paths = [
                "/usr/share/fonts/cambria.ttf",
                "/usr/share/fonts/cambria-bold.ttf",
            ]
            
            for font_path in font_paths:
                try:
                    title_font = ImageFont.truetype(font_path, size=60)      # Heading 55pt
                    date_font = ImageFont.truetype(font_path, size=25)       # Date text (will try bold below)
                    logo_font = ImageFont.truetype(font_path, size=16)       # Logo text
                    button_font = ImageFont.truetype(font_path, size=12)     # Button text
                    break
                except:
                    continue
            # Try to use a bold font specifically for the date
            try:
                bold_candidates = [
                    "/usr/share/fonts/cambria-bold.ttf",
                    "/usr/share/fonts/Cambria-Bold.ttf",
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                ]
                for bold_path in bold_candidates:
                    try:
                        date_font = ImageFont.truetype(bold_path, size=18)
                        break
                    except:
                        continue
            except:
                pass
            
            if not title_font:
                title_font = ImageFont.load_default()
                date_font = ImageFont.load_default()
                logo_font = ImageFont.load_default()
                button_font = ImageFont.load_default()
                
        except Exception as e:
            logger.warning(f"Font loading issue: {e}, using default")
            title_font = ImageFont.load_default()
            date_font = ImageFont.load_default()
            logo_font = ImageFont.load_default()
            button_font = ImageFont.load_default()
        
        # === TOP SECTION: Paste into white rounded area of template ===
        # Approximate white area by margins and height proportion from template
        margin = int(width * 0.027)  # ~24px for 900px
        top_section_y = margin
        top_section_height = int(height * 0.55) + 12.3  # increase image height by 20px
        # Decrease image width by 10px total (5px inset on each side) while centered
        inset = 4
        # Cut 5px height from the top of the image area
        top_cut = 5
        # Ensure integer geometry for PIL operations
        top_area = [
            int(margin + inset),
            int(top_section_y + top_cut),
            int((width - margin) - inset),
            int(top_section_y + top_section_height - margin)
        ]
        
        # Try to fetch real article image
        article_image_data = fetch_article_image(article_url)
        
        if article_image_data:
            try:
                # Load and process the article image
                article_img = Image.open(article_image_data)
                
                # Resize image to fit the top section
                img_width = width
                img_height = top_section_height
                
                # Calculate aspect ratio to maintain proportions
                img_aspect = article_img.width / article_img.height
                target_aspect = img_width / img_height
                
                if img_aspect > target_aspect:
                    # Image is wider, crop width
                    new_width = int(img_height * img_aspect)
                    article_img = article_img.resize((new_width, img_height), Image.Resampling.LANCZOS)
                    left = (new_width - img_width) // 2
                    article_img = article_img.crop((left, 0, left + img_width, img_height))
                else:
                    # Image is taller, crop height
                    new_height = int(img_width / img_aspect)
                    article_img = article_img.resize((img_width, new_height), Image.Resampling.LANCZOS)
                    top = (new_height - img_height) // 2
                    article_img = article_img.crop((0, top, img_width, top + img_height))
                
                # Paste the processed image into top_area with rounded mask
                target_w = int(top_area[2] - top_area[0])
                target_h = int(top_area[3] - top_area[1])
                # First, scale the article image to a fixed height (680px) to maximize quality
                pre_height = 680
                img_aspect = article_img.width / article_img.height
                pre_width = int(pre_height * img_aspect)
                article_img = article_img.resize((pre_width, pre_height), Image.Resampling.LANCZOS)
                # Then center-crop to the target area size
                if pre_width < target_w:
                    # If still too narrow, scale up to target width keeping aspect
                    pre_width = target_w
                    pre_height = int(pre_width / img_aspect)
                    article_img = article_img.resize((pre_width, pre_height), Image.Resampling.LANCZOS)
                left = int(max(0, (pre_width - target_w) // 2))
                top_crop = int(max(0, (pre_height - target_h) // 2))
                article_img = article_img.crop((left, top_crop, left + target_w, top_crop + target_h))
                # Rounded mask
                corner_radius = int(min(target_w, target_h) * 0.03)
                mask = Image.new("L", (target_w, target_h), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.rounded_rectangle([0, 0, target_w, target_h], radius=corner_radius, fill=255)
                image.paste(article_img, (int(top_area[0]), int(top_area[1])), mask)
                # Draw a red border around the pasted image with the same corner radius
                border_color = (220, 0, 0)
                border_width = 1
                draw.rounded_rectangle(
                    [
                        int(top_area[0]),
                        int(top_area[1]),
                        int(top_area[2]),
                        int(top_area[3])
                    ],
                    radius=corner_radius,
                    outline=border_color,
                    width=border_width
                )
                logger.info("✅ Article image loaded and processed successfully")
                
            except Exception as e:
                logger.error(f"Error processing article image: {e}")
                # Fallback to placeholder
                draw.rectangle(top_area, fill=(240, 240, 240))
                placeholder_text = "📰 NEWS IMAGE"
                placeholder_bbox = draw.textbbox((0, 0), placeholder_text, font=logo_font)
                placeholder_width = placeholder_bbox[2] - placeholder_bbox[0]
                placeholder_x = top_area[0] + (target_w - placeholder_width) // 2
                placeholder_y = top_area[1] + (target_h - 20) // 2
                draw.text((placeholder_x, placeholder_y), placeholder_text, fill=(150, 150, 150), font=logo_font)
        else:
            # No image found, use placeholder
            target_w = int(top_area[2] - top_area[0])
            target_h = int(top_area[3] - top_area[1])
            draw.rectangle(top_area, fill=(240, 240, 240))
            placeholder_text = "📰 NEWS IMAGE"
            placeholder_bbox = draw.textbbox((0, 0), placeholder_text, font=logo_font)
            placeholder_width = placeholder_bbox[2] - placeholder_bbox[0]
            placeholder_x = top_area[0] + (target_w - placeholder_width) // 2
            placeholder_y = top_area[1] + (target_h - 20) // 2
            draw.text((placeholder_x, placeholder_y), placeholder_text, fill=(150, 150, 150), font=logo_font)
            # Draw a red border around the placeholder area to match image border
            corner_radius = int(min(target_w, target_h) * 0.03)
            border_color = (220, 0, 0)
            border_width = 3
            draw.rounded_rectangle(
                [
                    int(top_area[0]),
                    int(top_area[1]),
                    int(top_area[2]),
                    int(top_area[3])
                ],
                radius=corner_radius,
                outline=border_color,
                width=border_width
            )
        # === MIDDLE BAR: Use template's bar; place date left and title bottom-aligned ===
        bar_height = int(height * 0.10)
        bar_y = top_area[3] + margin // 2
        # Date on the left (timezone-aware for Asia/Dhaka) - no time component
        if ZoneInfo:
            now_bd = datetime.now(ZoneInfo("Asia/Dhaka"))
            current_date = now_bd.strftime("%A | %d %B %Y")
        else:
            bd_time = time.localtime(time.time() + 6 * 3600)
            current_date = time.strftime("%A| %d %B %Y", bd_time)
        draw.text((margin + 20, bar_y + 40), current_date, fill="white", font=date_font)

        # Title positioned below the bar with width-aware wrapping (max 3 lines)
        title_side_inset = int(max(0, margin * 1.5))
        max_text_width = int(width - 2 * title_side_inset)
        words = title.split()
        lines = []
        current_line = ""
        word_index = 0
        max_lines = 3
        while word_index < len(words) and len(lines) < max_lines:
            word = words[word_index]
            test_line = (current_line + " " + word).strip()
            bbox = draw.textbbox((0, 0), test_line, font=title_font)
            test_line_width = bbox[2] - bbox[0]
            if test_line_width <= max_text_width:
                current_line = test_line
                word_index += 1
            else:
                if current_line:
                    lines.append(current_line)
                    current_line = ""
                else:
                    # Single long word: hard cut to fit width
                    # progressively trim until it fits
                    hard = word
                    while hard and draw.textbbox((0, 0), hard, font=title_font)[2] - draw.textbbox((0, 0), hard, font=title_font)[0] > max_text_width:
                        hard = hard[:-1]
                    lines.append(hard if hard else word[:1])
                    word_index += 1
        if current_line and len(lines) < max_lines:
            lines.append(current_line)
        # If not all words were placed, ellipsize the last line to indicate more
        if word_index < len(words) and lines:
            last = lines[-1]
            while True:
                bbox = draw.textbbox((0, 0), last + "…", font=title_font)
                if (bbox[2] - bbox[0]) <= max_text_width or len(last) == 0:
                    lines[-1] = (last + "…") if len(last) > 0 else "…"
                    break
                last = last[:-1]
        # Draw lines centered within the expanded width
        if lines:
            y = bar_y + bar_height + 28
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=title_font)
                line_width = bbox[2] - bbox[0]
                line_x = (width - line_width) // 2
                draw.text(
                    (line_x, y),
                    line,
                    fill="white",
                    font=title_font,
                    stroke_width=1,
                    stroke_fill="white",
                )
                y += (bbox[3] - bbox[1]) + 6
        
        # Removed the "Details in Comment" button per request
        
        # Save to BytesIO
        output = BytesIO()
        image.save(output, format="PNG", quality=95, optimize=True)
        output.seek(0)
        
        file_size = len(output.getvalue())
        logger.info(f"✅ News card created successfully! Size: {file_size} bytes")
        
        return output
        
    except Exception as e:
        logger.error(f"❌ Error creating news card: {e}")
        import traceback
        traceback.print_exc()
        return None


def load_posted_links():
    """Load previously posted links from file"""
    global posted_links
    try:
        with open(POSTED_LINKS_FILE, "r") as f:
            content = f.read().strip()
            if content:
                posted_links = set(json.loads(content))
            else:
                posted_links = set()
        logger.info(f"Loaded {len(posted_links)} previously posted links")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        posted_links = set()
        logger.info(f"No previous links found or invalid JSON, starting fresh: {e}")

def save_posted_links():
    """Save posted links to file"""
    try:
        with open(POSTED_LINKS_FILE, "w") as f:
            json.dump(list(posted_links), f, indent=2)
        logger.info(f"Saved {len(posted_links)} posted links")
    except Exception as e:
        logger.error(f"Failed to save posted links: {e}")

def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully"""
    logger.info("Shutting down bot...")
    save_posted_links()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
# ===== TELEGRAM FUNCTIONS =====
def send_telegram_photo_sync(chat_id, photo_data, caption):
    """Send photo to Telegram (synchronous version)"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    
    files = {
        'photo': ('news_card.png', photo_data, 'image/png')
    }
    
    data = {
        'chat_id': str(chat_id),
        'caption': caption,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, data=data, files=files, timeout=30)
        result = response.json()
        
        if response.status_code == 200 and result.get('ok'):
            logger.info(f"✅ Photo sent to {chat_id}")
            return True
        else:
            logger.error(f"❌ Telegram API Error: {result}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error sending photo: {e}")
        return False

def send_telegram_message_sync(chat_id, text):
    """Send text message to Telegram (synchronous version)"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    data = {
        'chat_id': str(chat_id),
        'text': text,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, data=data, timeout=10)
        result = response.json()
        
        if response.status_code == 200 and result.get('ok'):
            logger.info(f"✅ Message sent to {chat_id}")
            return True
        else:
            logger.error(f"❌ Telegram API Error: {result}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error sending message: {e}")
        return False

# ===== NEWS SCRAPING FUNCTIONS =====
def get_latest_news():
    """Scrape latest news from Bangladesh Guardian"""
    logger.info("🔍 Checking for latest news...")
    
    # Chrome options
    options = Options()
    options.headless = True
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
    chrome_bin = os.getenv("CHROME_BIN")
    if chrome_bin and os.path.exists(chrome_bin):
        options.binary_location = chrome_bin
    
    driver = None
    tmp_profile_dir = None
    try:
        # Use a unique user-data-dir to avoid profile lock conflicts on servers
        tmp_profile_dir = tempfile.mkdtemp(prefix="newsbot-chrome-")
        options.add_argument(f"--user-data-dir={tmp_profile_dir}")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        cache_root = os.getenv("WDM_CACHE", os.path.join(os.path.dirname(__file__), ".wdm"))
        os.makedirs(cache_root, exist_ok=True)
        service = ChromeService(ChromeDriverManager(path=cache_root).install())
        extra = os.getenv("CHROME_EXTRA_ARGS")
        if extra:
            for arg in extra.split():
                options.add_argument(arg)
        driver = webdriver.Chrome(service=service, options=options)
        driver.get(NEWS_URL)
        
        # Wait for content to load
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CLASS_NAME, "LatestNews"))
        )
        
        # Parse with BeautifulSoup
        soup = BeautifulSoup(driver.page_source, "html.parser")
        articles = soup.find_all("div", class_="LatestNews")
        
        if not articles:
            logger.warning("No articles found")
            return []
        
        logger.info(f"📰 Found {len(articles)} articles")
        
        news_list = []
        for article in articles:
            try:
                # Get link
                link_elem = article.find("a")
                if not link_elem or 'href' not in link_elem.attrs:
                    continue
                    
                link = link_elem['href']
                if link.startswith('/'):
                    link = "https://www.bangladeshguardian.com" + link
                
                # Get title
                title_elem = article.find("h3", class_="Title")
                if not title_elem:
                    continue
                    
                title = title_elem.get_text(strip=True)
                
                if title and link:
                    news_list.append({
                        'title': title,
                        'link': link
                    })
                    
            except Exception as e:
                logger.error(f"Error parsing article: {e}")
                continue
        
        logger.info(f"✅ Successfully parsed {len(news_list)} articles")
        return news_list
        
    except Exception as e:
        logger.error(f"❌ Error scraping news: {e}")
        return []
        
    finally:
        if driver:
            driver.quit()
        if tmp_profile_dir:
            shutil.rmtree(tmp_profile_dir, ignore_errors=True)

def process_and_send_news():
    """Process news and send to Telegram"""
    global posted_links
    
    # Get latest news
    news_articles = get_latest_news()
    
    if not news_articles:
        logger.info("No articles to process")
        return
    
    new_articles = 0
    
    for article in news_articles:
        title = article['title']
        link = article['link']
        
        # Skip if already posted
        if link in posted_links:
            continue
        
        logger.info(f"🆕 New article: {title[:60]}...")
        
        try:
            # Create image card
            image_data = create_professional_news_card(title, link)
            
            if image_data:
                # Prepare caption (sanitized)
                safe_title = sanitize_text(title)
                caption = f"📰 <b>{safe_title}</b>\n\n🔗 <a href='{link}'>Read Full Article</a>"
                
                # Send to main group
                success = send_telegram_photo_sync(TELEGRAM_CHAT_ID, image_data.getvalue(), caption)
                
                if success:
                    # Mark as posted
                    posted_links.add(link)
                    new_articles += 1
                    logger.info(f"✅ Article posted successfully")
                    
                    # Send debug notification
                    if DEBUG_CHAT_ID:
                        debug_msg = f"✅ Posted to group: {sanitize_text(title)[:50]}..."
                        send_telegram_message_sync(DEBUG_CHAT_ID, debug_msg)
                    
                    # Save progress
                    save_posted_links()
                    
                    # Wait between posts
                    time.sleep(3)
                else:
                    logger.error(f"Failed to send article: {title[:50]}...")
            else:
                logger.error(f"Failed to create image for: {title[:50]}...")
                
        except Exception as e:
            logger.error(f"Error processing article {title[:30]}...: {e}")
    
    if new_articles > 0:
        logger.info(f"🎉 Posted {new_articles} new articles!")
    else:
        logger.info("📰 No new articles to post")

# ===== MAIN FUNCTION =====
def main():
    """Main function"""
    logger.info("🚀 Starting Bangladesh Guardian News Bot")
    
    # Load previous data
    load_posted_links()
    
    # Send startup message
    if DEBUG_CHAT_ID:
        if ZoneInfo:
            now_bd = datetime.now(ZoneInfo("Asia/Dhaka"))
            bd_time = now_bd.strftime("%Y-%m-%d %H:%M:%S %Z")
        else:
            bd_time = time.strftime("%Y-%m-%d %H:%M:%S +06", time.localtime(time.time() + 6 * 3600))
        startup_msg = f"🤖 News Bot Started!\n📅 {bd_time}\n🔄 Check interval: {CHECK_INTERVAL//60} minutes"
        send_telegram_message_sync(DEBUG_CHAT_ID, startup_msg)
    
    # Startup test card disabled for production
    
    # Main loop
    while True:
        try:
            logger.info("🔄 Starting news check cycle...")
            process_and_send_news()
            
            logger.info(f"⏰ Waiting {CHECK_INTERVAL} seconds until next check...")
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("👋 Bot stopped by user")
            break
        except Exception as e:
            logger.error(f"❌ Error in main loop: {e}")
            time.sleep(60)  # Wait 1 minute before retrying

if __name__ == "__main__":
    main()