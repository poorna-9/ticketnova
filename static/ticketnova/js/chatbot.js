
const CHATBOT_URL = "/api/chatbot/";

let requiredFields = [];
let isSending = false;

const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const chatMessages = document.getElementById("chat-messages");
const sendButton = document.getElementById("send-btn");

function getCSRFToken() {
    return document.querySelector(
        'meta[name="csrf-token"]'
    )?.getAttribute("content");
}

function addMessage(message, sender = "assistant") {
    const wrapper = document.createElement("div");
    wrapper.className = `message ${sender}-message`;

    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.textContent = sender === "user" ? "U" : "T";

    const content = document.createElement("div");
    content.className = "message-content";
    content.textContent = message;

    wrapper.appendChild(avatar);
    wrapper.appendChild(content);

    chatMessages.appendChild(wrapper);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function showTyping() {
    const typing = document.createElement("div");
    typing.id = "typing-indicator";
    typing.className = "typing-indicator";
    typing.textContent = "TicketNova is thinking...";

    chatMessages.appendChild(typing);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function hideTyping() {
    document.getElementById("typing-indicator")?.remove();
}

async function sendMessage(query) {
    if (!query.trim() || isSending) return;

    isSending = true;
    sendButton.disabled = true;

    addMessage(query, "user");
    showTyping();

    try {
        const response = await fetch(CHATBOT_URL, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCSRFToken()
            },
            body: JSON.stringify({
                query: query,
                required_fields: requiredFields
            })
        });

        const result = await response.json();

        if (!response.ok) {
            throw new Error(
                result.message || "Unable to process your request."
            );
        }

        hideTyping();

        // Always store the latest orchestrator state.
        requiredFields = result.required_fields ?? [];

        addMessage(
            result.message || "Here is what I found.",
            "assistant"
        );

        // Update the right-side panel.
        renderResponse(result);

    } catch (error) {
        hideTyping();

        addMessage(
            error.message || "Something went wrong. Please try again.",
            "assistant"
        );

    } finally {
        isSending = false;
        sendButton.disabled = false;
        chatInput.focus();
    }
}

chatForm.addEventListener("submit", function (event) {
    event.preventDefault();

    const query = chatInput.value.trim();

    if (!query) return;

    chatInput.value = "";
    sendMessage(query);
});

document.querySelectorAll(".quick-action").forEach(button => {
    button.addEventListener("click", () => {
        sendMessage(button.dataset.query);
    });
});

document.getElementById("new-chat-btn").addEventListener("click", () => {
    /*
      This clears only frontend state.
      A true new conversation also needs a backend session-reset
      operation. We will add that separately.
    */
    requiredFields = [];

    chatMessages.innerHTML = "";

    addMessage(
        "Hello! Welcome to TicketNova. What would you like to book today?",
        "assistant"
    );

    document.getElementById("content-title").textContent =
        "Explore TicketNova";

    document.getElementById("content-subtitle").textContent =
        "Your bookings and available options will appear here.";

    document.getElementById("content-body").innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">🎟️</div>
            <h2>Your next journey starts here</h2>
            <p>Start a conversation with TicketNova.</p>
        </div>
    `;
});
