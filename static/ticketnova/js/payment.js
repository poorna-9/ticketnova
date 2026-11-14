
let paymentCountdownInterval = null;

function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    })[char]);
}

window.escapeHTML = window.escapeHTML || escapeHTML;

async function renderPayment(payment, result) {
    const content = document.getElementById("content-body");
    if (!content) return;

    if (paymentCountdownInterval) {
        clearInterval(paymentCountdownInterval);
        paymentCountdownInterval = null;
    }

    setContentHeading("Payment Checkout", "Your seats are locked. Please complete the payment to confirm your booking.");

    // Handle amount: convert from paise if in thousands, or use amount_in_rupees
    let amount = Number(payment.amount || 0);
    let formattedAmount = "0.00";
    if (payment.amount_in_rupees) {
        formattedAmount = String(payment.amount_in_rupees);
    } else if (amount > 0) {
        formattedAmount = (amount / 100).toFixed(2);
    }

    // Extract booking details
    const booking = result?.data?.booking || {};
    const show = booking.show || {};

    const title = payment.movie_title || payment.title || payment.item_name || show.movie || show.movie_title || result?.data?.movie?.title || "Ticket Booking";
    const theater = payment.theater || show.theater || "";
    const screen = payment.screen || show.screen || "";
    const showDate = payment.show_date || show.show_date || "";
    const startTime = payment.start_time || show.start_time || "";

    // Seat labels
    let seats = payment.seat_labels || payment.seat_codes || payment.seat_numbers || booking.seat_labels || [];
    if (typeof seats === "string") {
        try { seats = JSON.parse(seats); } catch (e) { seats = [seats]; }
    }
    const seatsFormatted = Array.isArray(seats) ? seats.join(", ") : String(seats || "-");

    // Remaining seconds for timer
    let remainingSeconds = Number(payment.remaining_seconds || 600);

    const venueParts = [theater, screen].filter(Boolean);
    const venueInfo = venueParts.join(" · ");
    const timeParts = [showDate, startTime].filter(Boolean);
    const timeInfo = timeParts.join(" · ");

    content.innerHTML = `
        <div class="payment-card">
            <div class="payment-card-header">
                <div>
                    <h3>${escapeHTML(title)}</h3>
                    ${venueInfo ? `<p style="font-size: 13px; color: #777f91; margin-top: 4px;">${escapeHTML(venueInfo)}</p>` : ""}
                    ${timeInfo ? `<p style="font-size: 13px; color: #777f91; margin-top: 2px;">${escapeHTML(timeInfo)}</p>` : ""}
                </div>
                <div class="payment-lock-badge">
                    <span>🔒 Seats Locked</span>
                </div>
            </div>

            <div class="payment-detail-rows">
                <div class="payment-detail-row">
                    <span>Selected Seats:</span>
                    <strong>${escapeHTML(seatsFormatted)}</strong>
                </div>
                <div class="payment-detail-row">
                    <span>Order ID:</span>
                    <span style="font-family: monospace; font-size: 12px; color: #4b5262;">${escapeHTML(payment.razorpay_order_id || "-")}</span>
                </div>
                <div class="payment-detail-row">
                    <span>Seat Hold Timer:</span>
                    <span id="payment-timer-display" class="payment-timer">⏳ <span id="timer-countdown">10:00</span> remaining</span>
                </div>
                <div class="payment-total-row">
                    <span>Total Amount Payable:</span>
                    <span class="payment-total-amount">₹${escapeHTML(formattedAmount)}</span>
                </div>
            </div>

            <button type="button" id="pay_now" class="pay-now-btn primary-action">
                <span>💳 Pay Now · ₹${escapeHTML(formattedAmount)}</span>
            </button>

            <div id="payment-status-notice" class="payment-notice" style="display: none; margin-top: 10px;"></div>

            <p class="payment-notice">
                🔒 Safe & Secure Payment via Razorpay
            </p>
        </div>
    `;

    // Setup live countdown timer
    const countdownEl = document.getElementById("timer-countdown");
    const timerDisplay = document.getElementById("payment-timer-display");
    const payBtn = document.getElementById("pay_now");

    function updateTimer() {
        if (remainingSeconds <= 0) {
            clearInterval(paymentCountdownInterval);
            paymentCountdownInterval = null;
            if (countdownEl) countdownEl.textContent = "00:00";
            if (timerDisplay) {
                timerDisplay.className = "payment-timer expired";
                timerDisplay.textContent = "⚠️ Seat hold expired";
            }
            if (payBtn) {
                payBtn.disabled = true;
                payBtn.innerHTML = `<span>Seat Hold Expired</span>`;
            }
            const notice = document.getElementById("payment-status-notice");
            if (notice) {
                notice.style.display = "block";
                notice.style.color = "#e05252";
                notice.textContent = "Your seat hold has expired. Please select seats again to book.";
            }
            return;
        }

        const mins = Math.floor(remainingSeconds / 60);
        const secs = remainingSeconds % 60;
        if (countdownEl) {
            countdownEl.textContent = `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
        }
        remainingSeconds--;
    }

    updateTimer();
    paymentCountdownInterval = setInterval(updateTimer, 1000);

    // Attach click handler to Pay Now button
    if (payBtn) {
        payBtn.addEventListener("click", function () {
            payBtn.disabled = true;
            payBtn.innerHTML = `<span>⏳ Opening Payment Gateway...</span>`;
            openRazorpayCheckout(payment, result);
        });
    }
}


function openRazorpayCheckout(payment, result) {
    const payBtn = document.getElementById("pay_now");
    const notice = document.getElementById("payment-status-notice");

    const amount = Number(payment.amount || 0);
    const formattedAmount = payment.amount_in_rupees || (amount > 0 ? (amount / 100).toFixed(2) : "0.00");

    if (!window.Razorpay) {
        if (notice) {
            notice.style.display = "block";
            notice.style.color = "#e05252";
            notice.textContent = "Payment gateway could not be loaded. Please check your internet connection or disable ad-blockers.";
        }
        if (payBtn) {
            payBtn.disabled = false;
            payBtn.innerHTML = `<span>💳 Pay Now · ₹${escapeHTML(formattedAmount)}</span>`;
        }
        alert("Payment gateway could not be loaded.");
        return;
    }

    const options = {
        key: payment.razorpay_key_id || payment.key_id,
        amount: payment.amount,
        currency: payment.currency || "INR",
        order_id: payment.razorpay_order_id,

        name: "TicketNova",
        description: "Ticket Booking",

        handler: async function (response) {
            if (payBtn) {
                payBtn.innerHTML = `<span>⏳ Verifying payment...</span>`;
            }
            await verifyRazorpayPayment(
                payment,
                response,
                null
            );
        },

        modal: {
            ondismiss: function () {
                console.log("Payment window closed.");
                if (payBtn) {
                    payBtn.disabled = false;
                    payBtn.innerHTML = `<span>💳 Pay Now · ₹${escapeHTML(formattedAmount)}</span>`;
                }
                if (notice) {
                    notice.style.display = "block";
                    notice.style.color = "#777f91";
                    notice.textContent = "Payment window closed. Click Pay Now to complete your booking.";
                }
            }
        },

        theme: {
            color: "#6658e8"
        }
    };

    try {
        const checkout = new Razorpay(options);

        checkout.on("payment.failed", async function (response) {
            if (payBtn) {
                payBtn.disabled = false;
                payBtn.innerHTML = `<span>💳 Pay Now · ₹${escapeHTML(formattedAmount)}</span>`;
            }
            if (notice) {
                notice.style.display = "block";
                notice.style.color = "#e05252";
                notice.textContent = response.error?.description || "Payment failed. Click Pay Now to try again.";
            }
            await verifyRazorpayPayment(
                payment,
                null,
                response.error
            );
        });

        checkout.open();
    } catch (err) {
        console.error("Razorpay open error:", err);
        if (payBtn) {
            payBtn.disabled = false;
            payBtn.innerHTML = `<span>💳 Pay Now · ₹${escapeHTML(formattedAmount)}</span>`;
        }
    }
}

async function verifyRazorpayPayment(payment, response, error) {
    const payload = {
        session_id: payment.session_id,
        razorpay_order_id: payment.razorpay_order_id
    };

    if (response) {
        payload.razorpay_payment_id = response.razorpay_payment_id;
        payload.razorpay_signature = response.razorpay_signature;
    }

    if (error) {
        payload.error = error;
    }

    const verifyUrl = payment.verify_url || (
        payment.domain === "movie" ? "/movies/payments/verify/" :
        payment.domain === "bus" ? "/buses/payments/verify/" :
        payment.domain === "flight" ? "/flights/payments/verify/" :
        "/api/payments/verify/"
    );

    try {
        const responseFromServer = await fetch(
            verifyUrl,
            {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCSRFToken()
                },
                body: JSON.stringify(payload)
            }
        );

        const result = await responseFromServer.json();

        if (!responseFromServer.ok) {
            throw new Error(
                result.error?.message ||
                result.message ||
                "Payment verification failed."
            );
        }

        if (paymentCountdownInterval) {
            clearInterval(paymentCountdownInterval);
            paymentCountdownInterval = null;
        }

        showPaymentResult(result);

    } catch (error) {
        const payBtn = document.getElementById("pay_now");
        if (payBtn) {
            payBtn.disabled = false;
            const amount = Number(payment.amount || 0);
            const formattedAmount = payment.amount_in_rupees || (amount > 0 ? (amount / 100).toFixed(2) : "0.00");
            payBtn.innerHTML = `<span>💳 Pay Now · ₹${escapeHTML(formattedAmount)}</span>`;
        }
        addMessage(
            error.message || "Payment verification is pending.",
            "assistant"
        );
    }
}

function showPaymentResult(result) {
    const content = document.getElementById("content-body");

    const booking = result.data?.booking || result.data || result.booking || {};

    setContentHeading("Booking Confirmed! 🎉", "Your payment was successful.");

    const bookingCode = booking.booking_code || booking.ticket_id || booking.pnr || "Confirmed";

    const seats = Array.isArray(booking.seat_labels)
        ? booking.seat_labels.join(", ")
        : (booking.seat_labels || booking.seat_codes || booking.seat_numbers || "");

    const movieName = booking.show?.movie || booking.show?.movie_title || booking.movie || "";

    content.innerHTML = `
        <div class="booking-summary" style="border-left: 5px solid #23a66c;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 15px;">
                <span style="font-size: 28px;">✅</span>
                <div>
                    <h3 style="margin: 0; color: #16824f;">Payment Successful!</h3>
                    <p style="margin: 2px 0 0 0; color: #777f91; font-size: 13px;">Your booking has been confirmed.</p>
                </div>
            </div>

            <div style="background: #f8fafc; border-radius: 8px; padding: 14px; margin-top: 10px;">
                <p style="margin: 6px 0; font-size: 14px;"><strong>Booking Code:</strong> <span style="font-family: monospace; font-size: 15px; color: #252b40; font-weight: bold;">${escapeHTML(bookingCode)}</span></p>
                <p style="margin: 6px 0; font-size: 14px;"><strong>Status:</strong> <span class="status-label status-available">CONFIRMED</span></p>
                ${movieName ? `<p style="margin: 6px 0; font-size: 14px;"><strong>Movie:</strong> ${escapeHTML(movieName)}</p>` : ""}
                ${booking.show?.theater ? `<p style="margin: 6px 0; font-size: 14px;"><strong>Theater:</strong> ${escapeHTML(booking.show.theater)}</p>` : ""}
                ${booking.show?.screen ? `<p style="margin: 6px 0; font-size: 14px;"><strong>Screen:</strong> ${escapeHTML(booking.show.screen)}</p>` : ""}
                ${booking.show?.start_time ? `<p style="margin: 6px 0; font-size: 14px;"><strong>Showtime:</strong> ${escapeHTML(booking.show.start_time)}</p>` : ""}
                ${seats ? `<p style="margin: 6px 0; font-size: 14px;"><strong>Seats:</strong> ${escapeHTML(seats)}</p>` : ""}
            </div>
        </div>
    `;

    addMessage(
        `Your payment was successful! Your booking is confirmed with booking code: ${bookingCode}.`,
        "assistant"
    );
}
