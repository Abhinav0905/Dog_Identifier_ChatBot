// Dharamsala Animal Rescue Chatbot - Frontend

let sessionId = localStorage.getItem("dharmasala_session");
if (!sessionId) {
    sessionId = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
        var r = (Math.random() * 16) | 0;
        return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
    localStorage.setItem("dharmasala_session", sessionId);
}

let selectedFile = null;
let userLocation = null;

// Open chat button
var openChatBtn = document.getElementById("openChatBtn");
var chatContainer = document.querySelector(".chat-container");
var landing = document.querySelector(".landing");

var minimizeBtn = document.getElementById("minimizeBtn");

openChatBtn.addEventListener("click", function () {
    chatContainer.classList.remove("hidden");
    landing.classList.add("hidden");
});

minimizeBtn.addEventListener("click", function () {
    chatContainer.classList.add("hidden");
    landing.classList.remove("hidden");
});

// Grab DOM elements
var chatMessages = document.getElementById("chatMessages");
var messageInput = document.getElementById("messageInput");
var typingIndicator = document.getElementById("typingIndicator");
var uploadPreview = document.getElementById("uploadPreview");
var previewImg = document.getElementById("previewImg");
var fileNameSpan = document.getElementById("fileName");
var locationBar = document.getElementById("locationBar");
var locationText = document.getElementById("locationText");
var fileInput = document.getElementById("fileInput");
var sendBtn = document.getElementById("sendBtn");
var cameraBtn = document.getElementById("cameraBtn");
var locationBtn = document.getElementById("locationBtn");
var removeBtn = document.getElementById("removeBtn");

// --- Event listeners ---

sendBtn.addEventListener("click", sendMessage);

cameraBtn.addEventListener("click", function () {
    fileInput.click();
});

locationBtn.addEventListener("click", requestLocation);

removeBtn.addEventListener("click", removeImage);

fileInput.addEventListener("change", handleFileSelect);

messageInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

messageInput.addEventListener("input", function () {
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight, 120) + "px";
});

// --- File handling ---

function handleFileSelect(e) {
    var file = e.target.files[0];
    if (!file) return;
    if (!file.type.startsWith("image/")) {
        alert("Please select an image file.");
        return;
    }
    if (file.size > 10 * 1024 * 1024) {
        alert("Image must be under 10MB.");
        return;
    }
    selectedFile = file;
    var reader = new FileReader();
    reader.onload = function (ev) {
        previewImg.src = ev.target.result;
        fileNameSpan.textContent = file.name;
        uploadPreview.classList.add("active");
    };
    reader.readAsDataURL(file);
}

function removeImage() {
    selectedFile = null;
    uploadPreview.classList.remove("active");
    fileInput.value = "";
    previewImg.src = "";
    fileNameSpan.textContent = "";
}

// --- Geolocation ---

function requestLocation() {
    if (!navigator.geolocation) {
        alert("Geolocation is not supported by your browser.");
        return;
    }
    locationBtn.disabled = true;
    navigator.geolocation.getCurrentPosition(
        function (pos) {
            userLocation = {
                lat: pos.coords.latitude,
                lng: pos.coords.longitude,
                accuracy: pos.coords.accuracy,
            };
            locationBar.classList.add("active");
            locationText.textContent =
                pos.coords.latitude.toFixed(4) + ", " + pos.coords.longitude.toFixed(4);
            locationBtn.disabled = false;
        },
        function (err) {
            alert("Unable to get location: " + err.message + "\nYou can describe the location in your message.");
            locationBtn.disabled = false;
        }
    );
}

// --- Send message ---

function sendMessage() {
    var text = messageInput.value.trim();
    if (!text && !selectedFile) return;

    // Show user message in chat
    if (text) {
        addMessage("user", text);
    }
    if (selectedFile) {
        addImageMessage("user", previewImg.src, selectedFile.name);
    }

    // Clear input
    messageInput.value = "";
    messageInput.style.height = "auto";
    showTyping(true);

    // Capture file reference before clearing
    var fileToSend = selectedFile;

    // Clear file selection immediately so UI updates
    removeImage();

    // Send request
    var promise;
    if (fileToSend) {
        promise = sendImageTriage(fileToSend, text);
    } else {
        promise = sendChatQuery(text);
    }

    promise
        .then(function (data) {
            showTyping(false);
            addAssistantResponse(data);
        })
        .catch(function (err) {
            showTyping(false);
            addMessage(
                "assistant",
                "Sorry, something went wrong. Please try again or contact rescue services directly if this is urgent."
            );
            console.error(err);
        });
}

// --- API calls ---

function sendImageTriage(file, context) {
    var formData = new FormData();
    formData.append("image", file);
    formData.append("context", context || "");
    formData.append("session_id", sessionId);
    if (userLocation) {
        formData.append("lat", userLocation.lat);
        formData.append("lng", userLocation.lng);
        formData.append("location_source", "browser");
    }
    return fetch("/v1/triage/image", { method: "POST", body: formData }).then(function (res) {
        if (!res.ok) throw new Error("Triage request failed: " + res.status);
        return res.json();
    });
}

function sendChatQuery(message) {
    return fetch("/v1/chat/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message, session_id: sessionId }),
    }).then(function (res) {
        if (!res.ok) throw new Error("Chat request failed: " + res.status);
        return res.json();
    });
}

// --- Chat rendering ---

function addMessage(role, text) {
    var div = document.createElement("div");
    div.className = "message " + role;
    var avatar = role === "assistant" ? "&#128054;" : "&#128100;";
    div.innerHTML =
        '<div class="message-avatar">' + avatar + "</div>" +
        '<div class="message-bubble">' + renderMarkdown(text) + "</div>";
    chatMessages.appendChild(div);
    scrollToBottom();
}

function addImageMessage(role, src, name) {
    var div = document.createElement("div");
    div.className = "message " + role;
    div.innerHTML =
        '<div class="message-avatar">&#128100;</div>' +
        '<div class="message-bubble">' +
        '<img class="image-preview" src="' + escapeAttr(src) + '" alt="' + escapeAttr(name) + '">' +
        "</div>";
    chatMessages.appendChild(div);
    scrollToBottom();
}

function addAssistantResponse(data) {
    var div = document.createElement("div");
    div.className = "message assistant";

    var content = renderMarkdown(data.response || "No response received.");

    // Triage severity badge
    if (data.triage && data.triage.severity) {
        var sev = data.triage.severity;
        content +=
            '<div><span class="triage-badge ' + sev + '">' +
            sev + " severity (" + data.triage.severity_score +
            "/10) &bull; " + Math.round(data.triage.confidence * 100) +
            "% confidence</span></div>";
    }

    // Case ID
    if (data.incident_id) {
        content +=
            '<div style="margin-top:8px;font-size:11px;color:#999;">Case ID: ' +
            data.incident_id.substring(0, 8) + "...</div>";
    }

    div.innerHTML =
        '<div class="message-avatar">&#128054;</div>' +
        '<div class="message-bubble">' + content + "</div>";
    chatMessages.appendChild(div);
    scrollToBottom();
}

// --- Helpers ---

function renderMarkdown(text) {
    if (!text) return "";
    var html = text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/\*(.+?)\*/g, "<em>$1</em>")
        .replace(/^(\d+)\.\s+(.+)$/gm, "<li>$2</li>")
        .replace(/^- (.+)$/gm, "<li>$1</li>")
        .replace(/\n/g, "<br>");
    // Wrap consecutive <li> items in <ol>
    html = html.replace(/((?:<li>.*?<\/li>(?:<br>)?)+)/g, function (match) {
        var items = match.replace(/<br>/g, "");
        return "<ol>" + items + "</ol>";
    });
    return html;
}

function escapeAttr(str) {
    return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

function showTyping(show) {
    typingIndicator.classList.toggle("active", show);
    if (show) scrollToBottom();
}

function scrollToBottom() {
    setTimeout(function () {
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }, 50);
}
