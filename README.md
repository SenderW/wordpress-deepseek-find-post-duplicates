# WordPress Duplicate Title Cleanup with DeepSeek

This repository contains an example Python script that helps to find and clean up possible duplicate WordPress posts based on their titles. It uses the WordPress REST API to fetch posts and the DeepSeek API to group titles that might describe the same content.

By default, the script runs in **dry run** mode. It does not delete anything unless you explicitly enable deletion with a command line flag.

---

## Features

- Fetches all `publish` and `future` posts from a WordPress site via REST API  
- Sends titles to DeepSeek to get groups of possibly duplicate posts  
- For each group, shows:
  - Post ID  
  - Title  
  - URL  
  - Status and date  
  - Indicator if a featured image is set  
- Interactive actions per group:
  - Keep one post and (optionally) delete the others  
  - Skip a group  
  - Cancel and exit the script  
- Dry run mode by default; actual deletion only with `--delete`

---

## Files

- `duplicate_title_cleanup.py` – main script  
- `requirements.txt` – Python dependencies  
- `.env.example` – example for required environment variables  

---

## Requirements

- Python 3.9 or newer  
- A WordPress site with REST API access  
- A WordPress application password with permission to read and delete posts  
- A DeepSeek API key  

---

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/your-account/wordpress-deepseek-duplicate-titles.git
   cd wordpress-deepseek-duplicate-titles
