# Bengali Subtitle Generation System

A desktop-based **AI-powered Bengali Subtitle Generation System** that allows users to watch local video files or YouTube videos with automatically generated Bengali subtitles. The system extracts audio from a video, performs speech-to-text transcription, translates the transcript into Bengali, generates an SRT subtitle file, and displays synchronized subtitles over the video.

This project was developed as a **Final Year B.Tech Computer Science project** to make video content more accessible for Bengali-speaking users.

---

## Table of Contents

- [Overview](#overview)
- [Problem Statement](#problem-statement)
- [Objectives](#objectives)
- [Key Features](#key-features)
- [System Architecture](#system-architecture)
- [Workflow](#workflow)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Installation and Setup](#installation-and-setup)
- [Environment Variables](#environment-variables)
- [How to Run](#how-to-run)
- [API Endpoints](#api-endpoints)
- [Modules Explained](#modules-explained)
- [Output](#output)
- [Challenges Faced](#challenges-faced)
- [Future Scope](#future-scope)
- [Contributors](#contributors)
- [Acknowledgement](#acknowledgement)
- [License](#license)

---

## Overview

The **Bengali Subtitle Generation System** is designed to generate Bengali subtitles for video content automatically. Users can either upload a local video file or provide a YouTube/video URL. The application processes the audio, detects speech segments, translates the content into Bengali, generates subtitle timestamps, and displays the subtitles in real time while the video plays.

The system supports both:

- **Online mode** using Groq API for transcription and translation.
- **Offline mode** using local models such as Faster-Whisper and NLLB-200.

---

## Problem Statement

Many videos are available only in English or other languages, which creates a language barrier for Bengali-speaking audiences. Manually creating Bengali subtitles is time-consuming, costly, and requires technical knowledge.

This project solves the problem by providing an automated system that can generate Bengali subtitles from video audio and display them in a synchronized video player.

---

## Objectives

The main objectives of this project are:

- To accept a local video file or YouTube/video URL as input.
- To extract and convert video audio using FFmpeg.
- To transcribe speech from audio using AI-based speech recognition.
- To translate the transcript into natural Bengali.
- To generate an SRT subtitle file with accurate timestamps.
- To display synchronized Bengali subtitles on the video.
- To support speaker labeling using speaker diarization.
- To provide both online and offline processing support.

---

## Key Features

- Upload and process local video files.
- Process YouTube or direct video URLs.
- Automatic audio extraction using FFmpeg.
- AI-based speech-to-text transcription.
- English or multilingual speech translation to Bengali.
- SRT subtitle file generation.
- Real-time subtitle overlay on video.
- Speaker identification support with labels such as `Person 1`, `Person 2`, etc.
- Subtitle customization options such as font size, subtitle position, and background toggle.
- Online/offline mode detection.
- Job progress tracking with percentage status.
- Local caching for processed audio and video.

---

## System Architecture

```text
User Input
   |
   |-- Local Video File / YouTube URL
   |
Electron Frontend
   |
   |-- Sends request to FastAPI backend
   |
FastAPI Backend
   |
   |-- FFmpeg Audio Extraction
   |-- Audio Chunking
   |-- Speech Recognition
   |-- Speaker Diarization
   |-- Bengali Translation
   |-- SRT Subtitle Generation
   |
Generated Bengali Subtitle File
   |
Electron Video Player
   |
Synchronized Bengali Subtitle Display
```

---

## Workflow

### 1. Input Module

The system accepts two types of input:

- Local video file such as `.mp4`, `.mkv`, `.avi`, `.mov`, `.webm`, etc.
- YouTube or direct video URL.

### 2. Audio Processing

The backend uses **FFmpeg** to extract audio from the selected video and convert it into MP3 format. The audio is converted to mono channel and 16 kHz sample rate for better speech recognition performance.

### 3. Audio Chunking

Large audio files are divided into smaller chunks. This helps the system process long videos efficiently and avoids API size limitations.

### 4. Speech Recognition

The system converts speech into text using:

- **Groq Whisper API** in online mode.
- **Faster-Whisper local model** in offline mode.

### 5. Speaker Detection

The system supports speaker diarization using:

- **Pyannote Audio** when a Hugging Face token is available.
- **VAD-based fallback** when pyannote is not available.

This allows subtitles to include speaker labels such as `Person 1:` and `Person 2:`.

### 6. Translation

The transcribed text is translated into Bengali using:

- **Groq Llama model** in online mode.
- **Facebook NLLB-200 model** in offline mode.

### 7. Subtitle Generation

The translated Bengali text is converted into `.srt` format with proper timestamps.

### 8. Subtitle Display

The Electron frontend parses the generated SRT file and displays Bengali subtitles over the video in synchronization with playback time.

---

## Tech Stack

### Frontend

- Electron.js
- HTML5
- CSS3
- JavaScript

### Backend

- Python
- FastAPI
- Uvicorn
- Pydantic

### AI / ML Models and APIs

- Whisper / Faster-Whisper
- Groq API
- Llama 3.3 model
- Facebook NLLB-200 translation model
- Pyannote Audio for speaker diarization

### Tools and Libraries

- FFmpeg
- yt-dlp
- httpx
- transformers
- sentencepiece
- torch
- python-dotenv
- ctranslate2

---

## Project Structure

```text
Bengali-subtitle-generation/
│
├── backend/
│   ├── without_face_detection.py     # Main FastAPI backend and subtitle pipeline
│   └── download_models.py            # Downloads local offline models
│
├── frontend/
│   ├── index.html                    # Main Electron UI
│   ├── main.js                       # Electron main process
│   ├── preload.js                    # Secure IPC bridge
│   ├── renderer.js                   # Frontend logic and subtitle rendering
│   └── styles.css                    # Application styling
│
├── package.json                      # Electron app configuration and scripts
├── package-lock.json                 # Node dependency lock file
├── requirements.txt                  # Python dependencies
├── .gitignore
└── README.md
```

---

## Installation and Setup

### Prerequisites

Make sure the following are installed on your system:

- Python 3.9 or above
- Node.js and npm
- FFmpeg
- Git

### 1. Clone the Repository

```bash
git clone https://github.com/your-username/Bengali-subtitle-generation.git
cd Bengali-subtitle-generation
```

### 2. Install Node.js Dependencies

```bash
npm install
```

### 3. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 4. Install FFmpeg

#### Windows

```bash
winget install ffmpeg
```

#### macOS

```bash
brew install ffmpeg
```

#### Linux

```bash
sudo apt install ffmpeg
```

---

## Environment Variables

Create a `.env` file inside the `backend/` folder:

```env
GROQ_API_KEY=your_groq_api_key_here
HF_TOKEN=your_huggingface_token_here
```

### Notes

- `GROQ_API_KEY` is required for online transcription and translation.
- `HF_TOKEN` is optional and is used for pyannote-based speaker diarization.
- If no internet connection is available, the system can use local models after downloading them.

---

## Offline Model Setup

To use the project in offline mode, run the following command once while connected to the internet:

```bash
python backend/download_models.py
```

This downloads:

- Faster-Whisper model
- Facebook NLLB-200 translation model

After the models are downloaded, the system can run using local models.

---

## How to Run

Start the Electron application:

```bash
npm start
```

The Electron application will automatically start the FastAPI backend.

### Optional: Run Backend Manually

```bash
python backend/without_face_detection.py
```

Backend server will run at:

```text
http://localhost:8000
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Checks backend health, mode, API key status, FFmpeg path, and model details |
| `GET` | `/mode` | Returns whether the system is running in online or offline mode |
| `POST` | `/transcribe` | Starts subtitle generation for a YouTube or direct video URL |
| `POST` | `/transcribe/file` | Starts subtitle generation for an uploaded local video file |
| `GET` | `/jobs/{job_id}` | Returns processing status and progress of a job |
| `GET` | `/download/{job_id}` | Downloads the generated Bengali SRT file |
| `GET` | `/video/{job_id}` | Streams the processed video for URL-based input |

---

## Modules Explained

### Electron Frontend

The frontend provides the user interface where users can select a local video file or enter a video URL. It also displays the video, shows processing status, and renders Bengali subtitles on top of the video.

### FastAPI Backend

The backend handles the complete subtitle generation pipeline, including audio extraction, transcription, translation, speaker labeling, subtitle creation, and file download.

### FFmpeg Processing

FFmpeg extracts audio from video and converts it into a format suitable for AI-based speech recognition.

### Speech Recognition Module

This module converts speech into text using Whisper-based models.

### Translation Module

This module translates the recognized speech into Bengali using either online Groq models or local NLLB models.

### Speaker Diarization Module

This module identifies different speakers and assigns labels such as `Person 1` and `Person 2`.

### SRT Generation Module

This module generates subtitle blocks with:

- Subtitle index
- Start time
- End time
- Bengali subtitle text
- Optional speaker label

Example:

```srt
1
00:00:01,000 --> 00:00:04,000
Person 1:
আমি আজকের বিষয়টি ব্যাখ্যা করব।
```

---

## Output

The final output of the system is:

- A Bengali subtitle file in `.srt` format.
- Bengali subtitles displayed in real time over the video.
- Speaker-labeled subtitles when speaker detection is available.

Generated subtitle files are stored in:

```text
srt_outputs/
```

---

## Challenges Faced

Some major challenges handled in this project include:

- Processing long videos without exceeding API limits.
- Maintaining accurate timestamps after audio chunking.
- Handling online and offline model switching.
- Generating readable Bengali subtitles with proper line wrapping.
- Supporting local video files securely inside Electron.
- Handling YouTube video download and playback.
- Detecting multiple speakers and assigning stable speaker labels.

---

## Future Scope

The project can be improved further by adding:

- Support for more target languages.
- Manual subtitle editing before export.
- Export options for `.vtt`, `.txt`, and burned-in subtitles.
- Better speaker diarization accuracy.
- GPU acceleration for faster offline processing.
- Cloud deployment for remote subtitle generation.
- User authentication and subtitle history storage.
- Batch processing for multiple videos.

---

## Contributors

- Tapan Manna
- Madhushree Ghosh
- Rajshree Ghosh 
- Soumyashis Dutta Gupta 
- Project Group No. 29

---

## Acknowledgement

We would like to express our sincere gratitude to our project guide, department faculty members, and institute for their continuous guidance and support throughout the development of this project.

We also acknowledge the open-source tools and libraries used in this project, including FastAPI, Electron.js, FFmpeg, Whisper, Faster-Whisper, NLLB-200, Pyannote Audio, yt-dlp, and other supporting technologies.

---

## License

This project is created for academic and learning purposes. You may update this section with an appropriate license such as MIT, Apache 2.0, or GPL based on your requirement.

---

## Repository Description

**AI-powered desktop application that generates synchronized Bengali subtitles for local and YouTube videos using speech recognition, translation, speaker diarization, FastAPI, and Electron.js.**
