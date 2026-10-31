// Dharamsala Animal Rescue Chatbot - Frontend

// --- Conversation lifecycle ---

var CONVERSATION_STORAGE_KEY = "dharmasala_session";
var WELCOME_BUBBLE_HTML =
    '<div class="message-avatar">&#128054;</div>' +
    '<div class="message-bubble welcome-bubble">' +
    "<p><strong>Thanks for visiting my page!</strong></p>" +
    "<p>My name is Dorjee, and I was rescued by Dharamsala Animal Rescue. My days are now filled with fun hikes, yummy food, and lots of belly rubs. But I was born on the street, hit by a car, and lost one of my legs.</p>" +
    "<p>Now, I want to help you understand community dogs, stay safe and be kind around them, and find animal-care options across India.</p>" +
    "<p><strong>Here are some things you can ask me:</strong></p>" +
    "<ul>" +
    "<li>How can I stay safe around community dogs?</li>" +
    "<li>Should I feed the dogs near my home?</li>" +
    "<li>I found a sick or injured dog. What should I do? <em>You can upload a photo.</em></li>" +
    "<li>I saw someone hurting an animal. Is this against the law?</li>" +
    "<li>There are new puppies near my school. How can I help them?</li>" +
    "</ul>" +
    "</div>";

let sessionId = null;
let conversationReady = false;
let requestInFlight = false;
let locationRequestInFlight = false;

let selectedFile = null;
let selectedFileCanPreview = true;
let selectedPreviewSrc = "";
let selectedPreviewPromise = Promise.resolve("");
let userLocation = null;
var MAX_IMAGE_SIZE_MB = 100;
var MAX_VISIBLE_CHAT_MESSAGES = 10;

// Open chat button
var openChatBtn = document.getElementById("openChatBtn");
var chatContainer = document.querySelector(".chat-container");
var landing = document.querySelector(".landing");

var minimizeBtn = document.getElementById("minimizeBtn");
var newChatBtn = document.getElementById("newChatBtn");
var conversationStatus = document.getElementById("conversationStatus");

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
var mapActions = document.getElementById("mapActions");
var fileInput = document.getElementById("fileInput");
var sendBtn = document.getElementById("sendBtn");
var cameraBtn = document.getElementById("cameraBtn");
var removeBtn = document.getElementById("removeBtn");
var locationBtn = document.getElementById("locationBtn");
var vetMapBtn = document.getElementById("vetMapBtn");
var rescueMapBtn = document.getElementById("rescueMapBtn");

// --- Event listeners ---

sendBtn.addEventListener("click", sendMessage);
newChatBtn.addEventListener("click", startNewConversation);

cameraBtn.addEventListener("click", function () {
    fileInput.click();
});

removeBtn.addEventListener("click", removeImage);

fileInput.addEventListener("change", handleFileSelect);

locationBtn.addEventListener("click", requestLocation);

vetMapBtn.addEventListener("click", function () {
    openGoogleMapsSearch("animal rescue NGO");
});

rescueMapBtn.addEventListener("click", function () {
    openGoogleMapsSearch("animal rescue NGO");
});

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

updateInteractionControls();
initializeConversation();

function initializeConversation() {
    conversationReady = false;
    requestInFlight = false;
    setConversationStatus("Starting conversation…");
    updateInteractionControls();

    var storedConversationId = getStoredConversationId();
    var startup = storedConversationId
        ? loadConversation(storedConversationId)
            .then(function (data) {
                activateConversation(storedConversationId, data, true);
            })
            .catch(function (err) {
                if (!isMissingConversationError(err)) throw err;
                removeStoredConversationId();
                return createAndActivateConversation(false);
            })
        : createAndActivateConversation(false);

    return startup.catch(function (err) {
        console.error("Conversation initialization failed", err);
        conversationReady = false;
        requestInFlight = false;
        resetVisibleConversation([]);
        addMessage(
            "assistant",
            err.userMessage || "I could not start this conversation. Please refresh the page and try again."
        );
        setConversationStatus("Conversation unavailable", true);
        updateInteractionControls();
    });
}

function createAndActivateConversation(restored) {
    return createConversation().then(function (data) {
        var conversationId = String(data.conversation_id || data.id || "").trim();
        if (!conversationId) {
            throw new Error("Conversation API did not return a conversation_id");
        }
        activateConversation(conversationId, data, restored);
    });
}

function activateConversation(conversationId, data, restored) {
    sessionId = conversationId;
    setStoredConversationId(conversationId);
    resetVisibleConversation(Array.isArray(data.messages) ? data.messages : []);
    conversationReady = true;
    requestInFlight = false;
    setConversationStatus(restored ? "Conversation restored" : "Conversation ready");
    updateInteractionControls();
}

function startNewConversation() {
    if (!conversationReady || requestInFlight || !sessionId) return;

    var previousConversationId = sessionId;
    conversationReady = false;
    requestInFlight = true;
    setConversationStatus("Starting a new conversation…");
    updateInteractionControls();

    archiveConversation(previousConversationId)
        .catch(function (err) {
            if (!isMissingConversationError(err)) throw err;
        })
        .then(function () {
            sessionId = null;
            removeStoredConversationId();
            resetClientCaseState();
            return createAndActivateConversation(false);
        })
        .catch(function (err) {
            console.error("Could not start a new conversation", err);
            conversationReady = Boolean(sessionId);
            requestInFlight = false;
            setConversationStatus("Could not start a new chat", true);
            updateInteractionControls();
            addMessage(
                "assistant",
                err.userMessage || (sessionId
                    ? "I could not start a new conversation. Your current conversation is still available."
                    : "The previous conversation was closed, but I could not start a new one. Please refresh the page.")
            );
        });
}

function createConversation() {
    return fetch("/v1/conversations", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: "{}",
    }).then(parseApiResponse);
}

function loadConversation(conversationId) {
    return fetch(
        "/v1/conversations/" + encodeURIComponent(conversationId) + "/messages",
        { credentials: "same-origin", cache: "no-store" }
    ).then(parseApiResponse);
}

function archiveConversation(conversationId) {
    return fetch(
        "/v1/conversations/" + encodeURIComponent(conversationId) + "/archive",
        { method: "POST", credentials: "same-origin" }
    ).then(parseApiResponse);
}

function resetVisibleConversation(messages) {
    chatMessages.innerHTML = "";
    renderWelcomeMessage();
    messages.forEach(renderRestoredMessage);
    scrollToBottom();
}

function renderWelcomeMessage() {
    var bubble = document.createElement("div");
    bubble.className = "message assistant";
    bubble.setAttribute("data-welcome-message", "true");
    bubble.innerHTML = WELCOME_BUBBLE_HTML;
    chatMessages.appendChild(bubble);
}

function trimVisibleConversation() {
    var messages = chatMessages.querySelectorAll(
        ".message:not([data-welcome-message])"
    );
    var overflow = messages.length - MAX_VISIBLE_CHAT_MESSAGES;
    for (var index = 0; index < overflow; index += 1) {
        messages[index].remove();
    }
}

function renderRestoredMessage(message) {
    if (!message || typeof message !== "object") return;
    var role = message.role === "assistant" ? "assistant" : message.role === "user" ? "user" : "";
    var content = String(message.content || message.message || message.response || "").trim();
    if (!role || !content) return;

    if (role === "assistant") {
        var metadata = parseMessageMetadata(message);
        addAssistantResponse({
            response: content,
            resource_links: normaliseStoredResourceLinks(message.resource_links || metadata.resource_links),
            location_verification: message.location_verification || metadata.location_verification || null,
        });
        return;
    }

    var uploadedImage = content.match(/^\[Image uploaded:\s*([^\]]+)\]\s*([\s\S]*)$/);
    if (uploadedImage) {
        if (uploadedImage[2]) addMessage("user", uploadedImage[2]);
        addImageMessage("user", "", uploadedImage[1]);
        return;
    }
    addMessage("user", content);
}

function parseMessageMetadata(message) {
    var metadata = message.metadata || message.metadata_json || {};
    if (typeof metadata === "string") {
        try {
            metadata = JSON.parse(metadata);
        } catch (_err) {
            metadata = {};
        }
    }
    return metadata && typeof metadata === "object" ? metadata : {};
}

function normaliseStoredResourceLinks(links) {
    if (typeof links === "string") {
        try {
            links = JSON.parse(links);
        } catch (_err) {
            links = [];
        }
    }
    return Array.isArray(links) ? links : [];
}

function resetClientCaseState() {
    removeImage();
    userLocation = null;
    locationBar.classList.remove("active");
    locationText.textContent = "Not shared";
    mapActions.classList.remove("active");
    messageInput.value = "";
    messageInput.style.height = "auto";
}

function updateInteractionControls() {
    var disabled = !conversationReady || requestInFlight;
    sendBtn.disabled = disabled;
    messageInput.disabled = disabled;
    cameraBtn.disabled = disabled;
    fileInput.disabled = disabled;
    locationBtn.disabled = disabled || locationRequestInFlight;
    newChatBtn.disabled = disabled || !sessionId;
}

function setConversationStatus(message, isError) {
    conversationStatus.textContent = message;
    conversationStatus.classList.toggle("error", Boolean(isError));
}

function getStoredConversationId() {
    try {
        return String(localStorage.getItem(CONVERSATION_STORAGE_KEY) || "").trim();
    } catch (_err) {
        return "";
    }
}

function setStoredConversationId(conversationId) {
    try {
        localStorage.setItem(CONVERSATION_STORAGE_KEY, conversationId);
    } catch (_err) {
        // The secure ownership cookie still protects this conversation. A browser
        // that blocks localStorage simply starts a new conversation after reload.
    }
}

function removeStoredConversationId() {
    try {
        localStorage.removeItem(CONVERSATION_STORAGE_KEY);
    } catch (_err) {
        // Nothing else is needed when storage is unavailable.
    }
}

function isMissingConversationError(err) {
    return err && (err.status === 404 || err.status === 410);
}

// --- File handling ---

function handleFileSelect(e) {
    var file = e.target.files[0];
    if (!file) return;
    var isHeic = /\.(heic|heif)$/i.test(file.name);
    if (!file.type.startsWith("image/") && !isHeic) {
        alert("Please select a JPEG, PNG, WebP, GIF, HEIC, or HEIF image.");
        return;
    }
    if (file.size > MAX_IMAGE_SIZE_MB * 1024 * 1024) {
        alert("Image must be under " + MAX_IMAGE_SIZE_MB + " MB.");
        return;
    }
    selectedFile = file;
    selectedFileCanPreview = !isHeic;
    selectedPreviewSrc = "";
    selectedPreviewPromise = Promise.resolve("");
    fileNameSpan.textContent = file.name;
    uploadPreview.classList.add("active");

    if (!selectedFileCanPreview) {
        previewImg.removeAttribute("src");
        previewImg.style.display = "none";
        fileNameSpan.textContent = file.name + " (converting preview...)";
        selectedPreviewPromise = requestImagePreview(file)
            .then(function (previewSrc) {
                if (selectedFile !== file) {
                    return previewSrc;
                }
                selectedPreviewSrc = previewSrc;
                selectedFileCanPreview = true;
                previewImg.src = previewSrc;
                previewImg.style.display = "";
                fileNameSpan.textContent = file.name;
                return previewSrc;
            })
            .catch(function (err) {
                if (selectedFile === file) {
                    fileNameSpan.textContent = file.name + " (preview unavailable, upload still works)";
                }
                console.error(err);
                return "";
            });
        return;
    }

    previewImg.style.display = "";
    var reader = new FileReader();
    selectedPreviewPromise = new Promise(function (resolve) {
        reader.onload = function (ev) {
            var previewSrc = ev.target.result || "";
            if (selectedFile === file) {
                selectedPreviewSrc = previewSrc;
                previewImg.src = previewSrc;
            }
            resolve(previewSrc);
        };
        reader.onerror = function () {
            resolve("");
        };
    });
    reader.readAsDataURL(file);
}

function removeImage() {
    selectedFile = null;
    selectedFileCanPreview = true;
    selectedPreviewSrc = "";
    selectedPreviewPromise = Promise.resolve("");
    uploadPreview.classList.remove("active");
    fileInput.value = "";
    previewImg.src = "";
    previewImg.style.display = "";
    fileNameSpan.textContent = "";
}

// --- Geolocation ---

function requestLocation() {
    if (!conversationReady || requestInFlight || locationRequestInFlight) return;
    if (!navigator.geolocation) {
        alert("Geolocation is not supported by your browser.");
        return;
    }
    if (!window.isSecureContext) {
        alert(
            "Location sharing is unavailable on this connection. You can still describe the town and state in your message."
        );
        return;
    }
    locationRequestInFlight = true;
    updateInteractionControls();
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
            updateMapActions();
            locationRequestInFlight = false;
            updateInteractionControls();
        },
        function (err) {
            alert("Unable to get location: " + err.message + "\nYou can upload a GPS-tagged photo or describe the location in your message.");
            locationRequestInFlight = false;
            updateInteractionControls();
        },
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 }
    );
}

// --- Send message ---

function sendMessage() {
    if (!conversationReady || requestInFlight || !sessionId) return;
    var text = messageInput.value.trim();
    if (!text && !selectedFile) return;

    var fileToSend = selectedFile;
    var fileNameToSend = fileToSend ? fileToSend.name : "";
    var previewPromise = selectedPreviewPromise || Promise.resolve(selectedPreviewSrc || "");

    if (text) {
        addMessage("user", text);
    }

    messageInput.value = "";
    messageInput.style.height = "auto";
    showTyping(true);
    requestInFlight = true;
    updateInteractionControls();

    var promise;
    if (fileToSend) {
        removeImage();
        promise = previewPromise
            .catch(function () {
                return "";
            })
            .then(function (previewSrc) {
                addImageMessage("user", previewSrc || "", fileNameToSend);
                return sendImageTriage(fileToSend, text);
            });
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
            if (isMissingConversationError(err)) {
                return recoverMissingConversationAfterSend().catch(function (recoveryErr) {
                    console.error("Could not recover expired conversation", recoveryErr);
                    addMessage(
                        "assistant",
                        recoveryErr.userMessage ||
                            "Your previous conversation expired, and I could not start a new one. Please refresh the page, then resend your last question."
                    );
                });
            }
            addMessage(
                "assistant",
                err.userMessage ||
                    "Sorry, something went wrong. Please try again or contact rescue services directly if this is urgent."
            );
            console.error(err);
        })
        .finally(function () {
            requestInFlight = false;
            updateInteractionControls();
            if (conversationReady) messageInput.focus();
        });
}

function recoverMissingConversationAfterSend() {
    conversationReady = false;
    sessionId = null;
    removeStoredConversationId();
    resetClientCaseState();
    setConversationStatus("Previous conversation expired. Starting a new one…");
    updateInteractionControls();

    return createConversation().then(function (data) {
        var conversationId = String(data.conversation_id || data.id || "").trim();
        if (!conversationId) {
            throw new Error("Conversation API did not return a conversation_id");
        }

        sessionId = conversationId;
        setStoredConversationId(conversationId);
        resetVisibleConversation([]);
        conversationReady = true;
        setConversationStatus("New conversation ready");
        addMessage(
            "assistant",
            "Your previous conversation expired, so I started a new one. Please resend your last question and reattach the photo if you included one."
        );
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
    return fetchWithTimeout("/v1/triage/image", {
        method: "POST",
        credentials: "same-origin",
        body: formData,
    }).then(parseApiResponse);
}

function requestImagePreview(file) {
    var formData = new FormData();
    formData.append("image", file);
    return fetch("/v1/image/preview", { method: "POST", body: formData }).then(function (res) {
        if (!res.ok) {
            throw new Error("Could not create image preview: " + res.status);
        }
        return res.blob();
    }).then(function (blob) {
        return blobToDataUrl(blob);
    });
}

function blobToDataUrl(blob) {
    return new Promise(function (resolve, reject) {
        var reader = new FileReader();
        reader.onload = function () {
            resolve(reader.result || "");
        };
        reader.onerror = reject;
        reader.readAsDataURL(blob);
    });
}

function sendChatQuery(message) {
    var payload = {
        message: message,
        session_id: sessionId,
    };
    if (userLocation) {
        payload.lat = userLocation.lat;
        payload.lng = userLocation.lng;
        payload.location_source = "browser";
    }
    return fetchWithTimeout("/v1/chat/query", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    }).then(parseApiResponse);
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
    trimVisibleConversation();
    scrollToBottom();
}

function addImageMessage(role, src, name) {
    var div = document.createElement("div");
    div.className = "message " + role;
    var imageContent = src
        ? '<img class="image-preview" src="' + escapeAttr(src) + '" alt="' + escapeAttr(name) + '">'
        : '<span class="image-file-label">&#128247; ' + escapeHtml(name) + "</span>";
    div.innerHTML =
        '<div class="message-avatar">&#128100;</div>' +
        '<div class="message-bubble">' +
        imageContent +
        "</div>";
    chatMessages.appendChild(div);
    trimVisibleConversation();
    scrollToBottom();
}

function addAssistantResponse(data) {
    var div = document.createElement("div");
    div.className = "message assistant";

    var content = renderMarkdown(data.response || "No response received.");

    content += renderResourceLinks(data.resource_links, data.response);

    div.innerHTML =
        '<div class="message-avatar">&#128054;</div>' +
        '<div class="message-bubble">' + content + "</div>";

    chatMessages.appendChild(div);
    trimVisibleConversation();
    scrollToBottom();
}

// --- Helpers ---

function fetchWithTimeout(url, options) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 120000);
    return fetch(url, Object.assign({}, options, { signal: controller.signal }))
        .catch(function (err) {
            if (err.name === "AbortError") {
                err.userMessage = "This request is taking too long. Please try again. If an animal needs urgent help, contact a nearby veterinarian directly.";
            }
            throw err;
        })
        .finally(function () { clearTimeout(timer); });
}

function parseApiResponse(res) {
    return res.text().then(function (text) {
        var data = null;
        if (text) {
            try {
                data = JSON.parse(text);
            } catch (e) {
                data = null;
            }
        }

        if (!res.ok) {
            var err = new Error("Request failed: " + res.status);
            err.status = res.status;
            err.userMessage = getApiErrorMessage(res, data);
            throw err;
        }

        return data || {};
    });
}

function getApiErrorMessage(res, data) {
    if (res.status === 429) {
        return "Please wait a minute before sending another message. If this is urgent, contact a nearby veterinarian directly.";
    }
    if (res.status >= 500) {
        return "I could not complete that request. Please try again. If this is urgent, contact a nearby veterinarian directly.";
    }
    if (res.status === 413) {
        return "That photo is too large to upload. Please choose an image under " + MAX_IMAGE_SIZE_MB + " MB or reduce the photo size and try again.";
    }

    if (data && data.detail) {
        if (typeof data.detail === "string") return data.detail;
        if (Array.isArray(data.detail) && data.detail.length && data.detail[0].msg) {
            return data.detail[0].msg;
        }
    }

    return "Sorry, something went wrong. Please try again or contact rescue services directly if this is urgent.";
}

function renderMarkdown(text) {
    if (!text) return "";
    // Escape HTML first
    var escaped = text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");

    // Process line-by-line so we can group consecutive list items correctly
    // and preserve the ORIGINAL numbering of ordered lists (fixes the
    // "serial numbers are off" bug where 3. 4. 5. would render as 1. 2. 3.).
    var lines = escaped.split(/\n/);
    var out = [];
    var i = 0;
    var orderedRe = /^(\d+)\.\s+(.+)$/;
    var bulletRe = /^[-*]\s+(.+)$/;

    while (i < lines.length) {
        var line = lines[i];
        var mOrd = line.match(orderedRe);
        var mBul = line.match(bulletRe);

        if (mOrd) {
            var startNum = parseInt(mOrd[1], 10);
            var items = [];
            while (i < lines.length) {
                var m = lines[i].match(orderedRe);
                if (!m) break;
                // Use explicit value=N to preserve gaps / non-1 starts.
                items.push('<li value="' + parseInt(m[1], 10) + '">' + m[2] + "</li>");
                i++;
            }
            out.push('<ol start="' + startNum + '">' + items.join("") + "</ol>");
            continue;
        }

        if (mBul) {
            var bItems = [];
            while (i < lines.length) {
                var mb = lines[i].match(bulletRe);
                if (!mb) break;
                bItems.push("<li>" + mb[1] + "</li>");
                i++;
            }
            out.push("<ul>" + bItems.join("") + "</ul>");
            continue;
        }

        out.push(line);
        i++;
    }

    var html = out.join("\n");

    // Inline formatting
    html = html
        // Angle-wrapped CommonMark destinations have already been HTML escaped.
        // Keep the captured URL escaped when inserting it into the attribute.
        .replace(/\[([^\]]+)\]\((?:&lt;(https?:\/\/[^\s]+?)&gt;|(https?:\/\/[^)\s]+))\)/g, function (_match, label, wrappedUrl, plainUrl) {
            var url = wrappedUrl || plainUrl;
            return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + "</a>";
        })
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/\*(.+?)\*/g, "<em>$1</em>");

    // The backend escapes punctuation in untrusted organization names and
    // evidence before composing Markdown. Our small renderer is not a full
    // CommonMark parser, so remove those escapes only after links/emphasis have
    // already been processed. Escaped content therefore stays plain text while
    // users no longer see strings such as "\\(IDA India\\)" or "rescue\\.".
    html = html.replace(/\\([\\`*_{}\[\]()#+.!<>|~-])/g, "$1");

    // Convert remaining newlines to <br>, but not inside list blocks
    html = html.replace(/\n+/g, function (m, offset, full) {
        // Avoid inserting <br> immediately around list tags
        var before = full.slice(Math.max(0, offset - 5), offset);
        var after = full.slice(offset + m.length, offset + m.length + 5);
        if (/<\/(ol|ul|li)>$/.test(before) || /^<(ol|ul|li)/.test(after)) {
            return "";
        }
        return "<br>";
    });

    return html;
}

function renderResourceLinks(links, responseText) {
    if (!Array.isArray(links) || !links.length) return "";

    var rendered = [];
    links.forEach(function (link) {
        if (!link || !link.url || !link.label) return;
        if (resourceLinkIsAlreadyInResponse(link, responseText)) return;
        try {
            var parsed = new URL(link.url, window.location.origin);
            if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return;
            var detailRows = [];
            var phone = String(link.phone || "").trim();
            var address = String(link.address || "").trim();
            var openingHours = String(link.opening_hours || "").trim();
            if (phone) {
                detailRows.push(
                    '<span class="resource-detail resource-phone">Phone: ' +
                    escapeHtml(phone) + "</span>"
                );
            }
            if (address) {
                detailRows.push(
                    '<span class="resource-detail">Address: ' + escapeHtml(address) + "</span>"
                );
            }
            if (openingHours) {
                detailRows.push(
                    '<span class="resource-detail">Hours: ' + escapeHtml(openingHours) + "</span>"
                );
            }
            rendered.push(
                '<div class="resource-card">' +
                '<a class="resource-link" href="' + escapeAttr(parsed.href) +
                '" target="_blank" rel="noopener noreferrer">' + escapeHtml(link.label) + "</a>" +
                detailRows.join("") +
                "</div>"
            );
        } catch (_err) {
            return;
        }
    });

    return rendered.length ? '<div class="resource-links">' + rendered.join("") + "</div>" : "";
}

function resourceLinkIsAlreadyInResponse(link, responseText) {
    var response = String(responseText || "");
    if (!response || !link) return false;

    var normalizedLabel = normalizeResourceText(link.label);
    var normalizedUrl = normalizeResourceUrl(link.url);
    if (!normalizedLabel || !normalizedUrl) return false;

    return response.split(/\n+/).some(function (block) {
        if (responseResourceUrls(block).indexOf(normalizedUrl) === -1) return false;

        var normalizedBlock = normalizeResourceText(block);
        if (!(" " + normalizedBlock + " ").includes(" " + normalizedLabel + " ")) {
            return false;
        }

        var phone = String(link.phone || "").trim();
        if (phone) {
            var phoneDigits = phone.replace(/\D/g, "");
            var blockDigits = block.replace(/\D/g, "");
            if (phoneDigits.length < 7 || blockDigits.indexOf(phoneDigits) === -1) {
                return false;
            }
        }

        return [link.address, link.opening_hours].every(function (detail) {
            var normalizedDetail = normalizeResourceText(detail);
            return (
                !normalizedDetail ||
                (" " + normalizedBlock + " ").includes(" " + normalizedDetail + " ")
            );
        });
    });
}

function responseResourceUrls(responseText) {
    var matches = String(responseText || "").match(/https?:\/\/[^\s<>)\]]+/g) || [];
    var urls = [];
    matches.forEach(function (value) {
        var normalized = normalizeResourceUrl(value.replace(/[.,;:!?]+$/, ""));
        if (normalized && urls.indexOf(normalized) === -1) urls.push(normalized);
    });
    return urls;
}

function normalizeResourceUrl(value) {
    try {
        var parsed = new URL(String(value || ""), window.location.origin);
        if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "";
        parsed.hash = "";
        var path = parsed.pathname.replace(/\/+$/, "") || "/";
        return parsed.origin.toLowerCase() + path + parsed.search;
    } catch (_err) {
        return "";
    }
}

function normalizeResourceText(value) {
    return String(value || "")
        .normalize("NFKC")
        .replace(/\\([\\`*_{}\[\]()#+.!<>|~-])/g, "$1")
        .toLocaleLowerCase()
        .replace(/[^\p{L}\p{N}]+/gu, " ")
        .trim();
}

function escapeAttr(str) {
    return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

function escapeHtml(str) {
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

function updateMapActions() {
    mapActions.classList.remove("active");
}

function openGoogleMapsSearch(query) {
    if (!userLocation) {
        alert("Share your location first so we can open nearby results.");
        return;
    }
    var nearbyQuery = query + " near " + userLocation.lat.toFixed(4) + "," + userLocation.lng.toFixed(4);
    window.open(
        "https://www.google.com/maps/search/?api=1&query=" + encodeURIComponent(nearbyQuery),
        "_blank",
        "noopener"
    );
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
