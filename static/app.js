// Dharmasala Animal Rescue Chatbot - Frontend
(function () {
    "use strict";

    let sessionId = localStorage.getItem("dharmasala_session") || crypto.randomUUID();
    localStorage.setItem("dharmasala_session", sessionId);

    let selectedFile = null;
    let userLocation = null;

    const chatMessages = document.getElementById("chatMessages");
    const messageInput = document.getElementById("messageInput");
    const typingIndicator = document.getElementById("typingIndicator");
    const uploadPreview = document.getElementById("uploadPreview");
    const previewImg = document.getElementById("previewImg");
    const fileNameSpan = document.getElementById("fileName");
    const locationBar = document.getElementById("locationBar");
    const locationText = document.getElementById("locationText");

    // Auto-resize textarea
    window.autoResize = function (el) {
        el.style.height = "auto";
        el.style.height = Math.min(el.scrollHeight, 120) + "px";
    };

    // Handle enter key
    window.handleKeyDown = function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    };

    // File selection
    window.handleFileSelect = function (e) {
        const file = e.target.files[0];
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
        const reader = new FileReader();
        reader.onload = function (ev) {
            previewImg.src = ev.target.result;
            fileNameSpan.textContent = file.name;
            uploadPreview.classList.add("active");
        };
        reader.readAsDataURL(file);
    };

    window.removeImage = function () {
        selectedFile = null;
        uploadPreview.classList.remove("active");
        document.getElementById("fileInput").value = "";
    };

    // Geolocation
    window.requestLocation = function () {
        if (!navigator.geolocation) {
            alert("Geolocation is not supported by your browser.");
            return;
        }
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
            },
            function () {
                alert("Unable to get location. You can describe the location in your message.");
            }
        );
    };

    // Send message
    window.sendMessage = async function () {
        const text = messageInput.value.trim();
        if (!text && !selectedFile) return;

        // Show user message
        if (text) {
            addMessage("user", text);
        }
        if (selectedFile) {
            addImageMessage("user", previewImg.src, selectedFile.name);
        }

        messageInput.value = "";
        messageInput.style.height = "auto";
        showTyping(true);

        try {
            let data;
            if (selectedFile) {
                data = await sendImageTriage(selectedFile, text);
            } else {
                data = await sendChatQuery(text);
            }
            showTyping(false);
            addAssistantResponse(data);
        } catch (err) {
            showTyping(false);
            addMessage(
                "assistant",
                "Sorry, something went wrong. Please try again or contact rescue services directly if this is urgent."
            );
            console.error(err);
        }

        removeImage();
    };

    async function sendImageTriage(file, context) {
        const formData = new FormData();
        formData.append("image", file);
        formData.append("context", context || "");
        formData.append("session_id", sessionId);
        if (userLocation) {
            formData.append("lat", userLocation.lat);
            formData.append("lng", userLocation.lng);
            formData.append("location_source", "browser");
        }
        const res = await fetch("/v1/triage/image", { method: "POST", body: formData });
        if (!res.ok) throw new Error("Triage request failed: " + res.status);
        return res.json();
    }

    async function sendChatQuery(message) {
        const res = await fetch("/v1/chat/query", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message, session_id: sessionId }),
        });
        if (!res.ok) throw new Error("Chat request failed: " + res.status);
        return res.json();
    }

    function addMessage(role, text) {
        const div = document.createElement("div");
        div.className = "message " + role;
        const avatar = role === "assistant" ? "&#128054;" : "&#128100;";
        div.innerHTML =
            '<div class="message-avatar">' +
            avatar +
            "</div>" +
            '<div class="message-bubble">' +
            renderMarkdown(text) +
            "</div>";
        chatMessages.appendChild(div);
        scrollToBottom();
    }

    function addImageMessage(role, src, name) {
        const div = document.createElement("div");
        div.className = "message " + role;
        div.innerHTML =
            '<div class="message-avatar">&#128100;</div>' +
            '<div class="message-bubble">' +
            '<img class="image-preview" src="' + src + '" alt="' + name + '">' +
            "</div>";
        chatMessages.appendChild(div);
        scrollToBottom();
    }

    function addAssistantResponse(data) {
        const div = document.createElement("div");
        div.className = "message assistant";

        let content = renderMarkdown(data.response || "No response received.");

        // Add triage badge if present
        if (data.triage && data.triage.severity) {
            const sev = data.triage.severity;
            content +=
                '<div><span class="triage-badge ' +
                sev +
                '">' +
                sev +
                " severity (" +
                data.triage.severity_score +
                "/10) &bull; " +
                Math.round(data.triage.confidence * 100) +
                "% confidence</span></div>";
        }

        // Add incident ID reference
        if (data.incident_id) {
            content +=
                '<div style="margin-top:8px;font-size:11px;color:#999;">Case ID: ' +
                data.incident_id.substring(0, 8) +
                "...</div>";
        }

        div.innerHTML =
            '<div class="message-avatar">&#128054;</div>' +
            '<div class="message-bubble">' +
            content +
            "</div>";
        chatMessages.appendChild(div);
        scrollToBottom();
    }

    function renderMarkdown(text) {
        if (!text) return "";
        // Simple markdown rendering
        return text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
            .replace(/\*(.+?)\*/g, "<em>$1</em>")
            .replace(/^(\d+)\.\s+(.+)$/gm, "<li>$2</li>")
            .replace(/^- (.+)$/gm, "<li>$1</li>")
            .replace(/\n/g, "<br>");
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
})();
