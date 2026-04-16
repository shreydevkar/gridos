# GridOS: Agentic Spreadsheet Operating System

GridOS is a high-performance spreadsheet engine architecture that integrates Large Language Models (LLMs) with a deterministic logic kernel. Unlike standard chatbots, GridOS agents possess **spatial awareness** and **transactional integrity**, allowing them to reason over two-dimensional data structures while adhering to strict mathematical and safety constraints.



## 🏗️ System Architecture

The platform is engineered using a decoupled, three-tier architecture to ensure reliability and scalability:

### 1. The Deterministic Kernel (`/core`)
The "Source of Truth" for the system. It manages state persistence and enforces grid-level constraints.
* **`engine.py`**: Handles memory allocation, coordinate mapping, and the resolution of write-collisions.
* **`models.py`**: Defines the strict Pydantic schemas for Agent Intents and Cell States.
* **`functions.py`**: A registry of atomic, validated mathematical operations.
* **`utils.py`**: Manages bidirectional translation between A1 notation and Cartesian coordinates.

### 2. The Orchestration Layer (`main.py`)
A FastAPI-driven middleware that acts as the system's "Thalamus."
* **Contextual Injection**: Streams live grid snapshots into the LLM context window.
* **Sandbox Routing**: Classifies incoming natural language into specialized agent domains (e.g., Financial Analysis vs. General Operations).
* **Safety Guardrails**: Validates AI-generated JSON payloads against physical cell locks before execution.

### 3. The Reactive Interface (`/static`)
A low-latency web interface designed for hybrid interaction.
* **Bi-directional Sync**: Reflects AI-driven changes in real-time while allowing manual user overrides.
* **Human-in-the-Loop**: Users can manually lock specific ranges to prevent AI modifications to critical templates.



---

## 🚀 Key Capabilities

* **Agentic Spatial Awareness**: Agents analyze occupied vs. vacant cells to optimize data placement.
* **Formula Synthesis**: Converts natural language prompts into executable grid formulas (e.g., `=MINUS(C3, D3)`).
* **Collision Resolution**: Intelligent shifting logic ensures data integrity when multiple agents or users target the same coordinates.
* **State Persistence**: Session data is serialized to `.gridos` files for long-term storage and recovery.

---

## 🛠️ Technical Specifications

| Layer | Technology |
| :--- | :--- |
| **Logic Kernel** | Python 3.10+ |
| **Inference Engine** | Google Gemini 1.5 Flash / Pro |
| **API Framework** | FastAPI (Asynchronous) |
| **Frontend** | HTML5 / Tailwind CSS / Vanilla JS |
| **Persistence** | Custom Serialization (.gridos) |

---

## 🧩 Developer Ecosystem & Telemetry

GridOS is designed as a modular platform, prioritizing transparency and extensibility.

### 📊 Telemetry & Governance
To support enterprise-grade deployments and cost management, GridOS includes built-in telemetry:
* **Token Attribution**: Every agent interaction is logged with precise metadata regarding Prompt and Completion tokens.
* **Resource Costing**: Enables administrators to track the USD cost of LLM inference per user or per specialized agent.
* **Audit Trails**: Maintains a ledger of all "Agent Intents" versus "Actual Writes," providing a clear history of how the grid reached its current state.

### 🔌 Extensibility (The SDK)
* **Agent Profiles**: Define new expertise by dropping JSON schemas into the `/agents` directory.
* **Custom Formula Registry**: Developers can register Python-backed formulas that the AI can then utilize within the grid logic.
---

## 🚦 Deployment Guide

### Prerequisites
* Python 3.10 or higher
* Valid Gemini API Credentials

### Installation
1.  **Clone the Repository:**
    ```bash
    git clone [https://github.com/your-org/gridos-kernel.git](https://github.com/your-org/gridos-kernel.git)
    cd gridos-kernel
    ```

2.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Environment Setup:**
    Configure your `GOOGLE_API_KEY` in the `main.py` configuration or as an environment variable.

4.  **Launch the Application:**
    ```bash
    uvicorn main:app --reload
    ```
    Access the interface at `http://127.0.0.1:8000`.

---

## 🗺️ Development Roadmap

- [x] **Phase 1: Deterministic Core** – Grid memory and collision logic.
- [x] **Phase 2: Agentic Routing** – Intent classification and JSON-based command execution.
- [x] **Phase 3: Hybrid Interface** – Reactive UI with manual/AI shared control.
- [ ] **Phase 4: Multi-Agent Swarm** – Integration of specialized "Scout" agents for real-time web data fetching.
- [ ] **Phase 5: Advanced Computation** – Range-based vector operations and cross-sheet referencing.

---

## 🗺️ Next Steps & Roadmap

1. **The Agentic Loop**: Implementing "Multi-Step Chaining" where an agent can write a value, observe the result of a formula, and then execute a follow-up action autonomously.
2. **The Marketplace Interface**: A UI gallery for users to browse, test, and activate community-contributed agents.
3. **External Connectors**: Direct REST API integration for pulling live data (Stock prices, Weather, CRM data) into grid cells.

---

## 🤝 Contributing

We welcome contributions from the community! 

* **Feature Requests**: Open an issue to suggest new core functions or agent types.
* **Pull Requests**: Ensure all core logic changes are accompanied by updates to the `test_harness.py`.
* **Agent Marketplace**: Submit your specialized `.json` agent profiles to our community gallery.

---
© 2024 GridOS Architecture Group. Licensed under the MIT License.

---

© 2024 GridOS Architecture Group. Licensed under the MIT License.
