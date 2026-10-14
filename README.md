# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management,
attendance tracking, attendance regularization, and analytics.

## Features

- Employee creation and paginated listing
- Attendance punch-in and punch-out
- Attendance filtering and regularization history
- Monthly employee attendance analytics
- Department summary and daily attendance trends
- Late-arrival leaderboard
- MongoDB indexes for uniqueness and query performance

## Requirements

- Python 3.10+
- MongoDB 6.0 or newer

## Setup

Install dependencies:

    pip install -r requirements.txt

Configure these environment variables:

- MONGO_URI
- MONGO_DB

Start the API from the project root:

    uvicorn app.main:app --port 8000

Open the API documentation at:

    http://127.0.0.1:8000/docs

## Database

The application uses MongoDB collections for employees and attendance logs.
Indexes are created at application startup.

## Configuration

Keep database credentials in environment variables.
Never commit `.env` files or secrets.