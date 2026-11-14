# TicketNova 🎟️

**An AI-powered multi-domain ticket booking platform for Movies, Bus Travel, and Flights.**

TicketNova is a conversational booking platform that allows users to search, explore, and book movie tickets, bus journeys, and flights through a unified AI-powered chatbot.

The system uses domain-specific AI agents coordinated by a central orchestrator. It combines LLM-based natural language understanding with deterministic backend APIs to ensure that booking operations, seat availability, payments, and transactional state remain under backend control.

## ✨ Key Features

### 🎬 Movie Booking
- Search for currently running movies.
- Explore available theatres and shows.
- View show-specific seat layouts.
- Select seats and proceed with bookings.
- Integrated payment workflow.

### 🚌 Bus Booking
- Search bus services by source, destination, and journey date.
- Filter trips based on travel preferences.
- Explore bus operators, timings, and fares.
- Select seats and manage bookings.
- Integrated payment workflow.

### ✈️ Flight Booking
- Search flights by origin, destination, and travel date.
- Filter flights by airline, cabin class, and departure time.
- View available seat layouts.
- Select passenger seats.
- Collect passenger information and process bookings.
- Support booking-related queries and booking management.

## 🧠 AI Architecture

TicketNova follows a **central orchestrator + domain-specific agent architecture**.

```text
                  User Message
                       |
                       v
              Central Orchestrator
                       |
              Domain Identification
                       |
          +------------+------------+
          |            |            |
          v            v            v
      Movie Agent   Bus Agent   Flight Agent
          |            |            |
          v            v            v
      Movie APIs    Bus APIs    Flight APIs
          |            |            |
          +------------+------------+
                       |
                       v
              Backend Response
                       |
                       v
                 User Response
```

### Core Design Principles

- **Centralized orchestration:** Identifies the appropriate domain and routes user messages to the corresponding agent.
- **Domain-specific agents:** Movie, bus, and flight agents maintain their own domain-specific workflows and booking logic.
- **LLM for language understanding:** Uses language models to interpret user messages and extract structured information.
- **Backend owns transactional truth:** Database operations, seat availability, booking confirmation, and payment processing are handled by backend services.
- **Multi-turn conversations:** Maintains conversational context to collect missing information and continue pending booking workflows.
- **Structured extraction:** Uses Pydantic schemas to validate and structure LLM outputs.

## 📊 AI Observability

TicketNova includes an AI observability layer to capture and inspect the execution of AI workflows.

The observability system records structured JSON events containing:

- Event ID and trace ID.
- Execution stage.
- Model information.
- Input and output metadata.
- Execution latency.
- Success or failure status.
- Error information.

Each request uses a trace ID to correlate events across different stages of the orchestration and agent execution.

Logs are written to:

```text
logs/ai_events.log
```

Log rotation is configured to manage file size and retain previous log files.

## 🛠️ Technology Stack

| Component | Technology |
|---|---|
| Backend | Python, Django, Django REST Framework |
| AI / LLM | LangChain, OpenAI |
| Structured Outputs | Pydantic |
| Database | SQLite (local development; configurable database backend) |
| Payments | Razorpay |
| Frontend | HTML, CSS, JavaScript |
| Containerization | Docker, Docker Compose |
| Logging | Python Logging, JSON-based AI Observability |

## 📁 Project Structure

```text
ticketnova/
│
├── buses/
│   ├── migrations/
│   ├── bus_agent.py
│   ├── bus_chat.py
│   ├── models.py
│   ├── payments.py
│   ├── serializer.py
│   └── views.py
│
├── flights/
│   ├── migrations/
│   ├── flight_agent.py
│   ├── flight_chat.py
│   ├── models.py
│   ├── payments.py
│   ├── serializer.py
│   └── views.py
│
├── movies/
│   ├── migrations/
│   ├── movie_agent.py
│   ├── moviechat.py
│   ├── models.py
│   ├── payments.py
│   ├── serializer.py
│   └── views.py
│
├── orchestrator/
│   ├── models.py
│   ├── urls.py
│   └── views.py
│
├── config/
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
│
├── ai_observability.py
├── static/
│   └── ticketnova/
├── templates/
│   └── ticketnova/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── manage.py
```

## 🚀 Getting Started

### Prerequisites

- Python 3.12+
- Docker and Docker Compose
- OpenAI API key
- Razorpay credentials (for payment integration)

### 1. Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/ticketnova.git
cd ticketnova
```

### 2. Create a Virtual Environment

```bash
python -m venv venv
```

Activate it:

**Windows:**
```powershell
venv\Scripts\activate
```

**Linux / macOS:**
```bash
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Create a `.env` file in the project root and configure the required credentials.

```env
SECRET_KEY=your_django_secret_key
DEBUG=True

OPENAI_API_KEY=your_openai_api_key

RAZORPAY_KEY_ID=your_razorpay_key_id
RAZORPAY_KEY_SECRET=your_razorpay_key_secret
```

Add any additional database or service configuration required by your local settings.

**Never commit `.env` or expose API credentials in the repository.**

### 5. Apply Database Migrations

```bash
python manage.py migrate
```

### 6. Start the Development Server

```bash
python manage.py runserver
```

Open:

```text
http://127.0.0.1:8000/
```

## 🐳 Running with Docker

Build and start the application using Docker Compose:

```bash
docker compose up --build
```

Run migrations inside the application container:

```bash
docker compose exec app python manage.py migrate
```

Stop the containers:

```bash
docker compose down
```

Refer to your Docker Compose configuration for the actual service names and exposed ports.

## 🔒 Security and Reliability

TicketNova is designed around the separation of AI interpretation and backend transaction execution.

- LLM outputs are structured and validated before use.
- Booking operations are handled through backend APIs.
- Seat availability and booking state are maintained by backend services.
- Payment processing is handled through dedicated payment modules.
- AI execution events support traceability and debugging.
- Credentials should be supplied through environment variables rather than committed to source control.

## 🗺️ Future Improvements

- Asynchronous AI evaluation using DeepEval.
- Evaluation metrics for individual agents and orchestration decisions.
- Expanded AI observability and analytics.
- Improved scalability and distributed task processing.
- Additional booking domains and integrations.

## 👨‍💻 Author

**Poorna Chandra**

GitHub: [poorna-9](https://github.com/poorna-9)

---

*TicketNova — One conversational interface for multiple booking experiences.*
