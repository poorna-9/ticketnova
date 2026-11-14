
function renderBusResponse(result) {
    const content = document.getElementById("content-body");
    const intent = result.intent;
    const data = result.data || {};

    if (intent === "show_bus_seats") {
        renderBusSeats(data.seat_map);
        return;
    }

    if (intent === "my_bus_bookings") {
        renderBusBookings(data.bookings || []);
        return;
    }

    if (data.trips) {
        renderBusTrips(data.trips);
        return;
    }

    if (data.booking) {
        const booking = data.booking;
        const seats = Array.isArray(booking.seat_codes)
            ? booking.seat_codes.join(", ")
            : (booking.seat_codes || booking.seats || "-");

        content.innerHTML = `
            <div class="booking-summary">
                <h3>Bus Booking Details</h3>
                ${booking.ticket_id || booking.booking_code ? `<p><strong>Ticket ID:</strong> ${escapeHTML(booking.ticket_id || booking.booking_code)}</p>` : ""}
                <p><strong>Status:</strong> <span class="status-label status-available">${escapeHTML(booking.status || "CONFIRMED")}</span></p>
                ${booking.bus_name || booking.trip ? `<p><strong>Bus:</strong> ${escapeHTML(booking.bus_name || booking.trip?.bus_name || "")}</p>` : ""}
                ${booking.boarding_point ? `<p><strong>Boarding Point:</strong> ${escapeHTML(booking.boarding_point)}</p>` : ""}
                <p><strong>Seats:</strong> ${escapeHTML(seats)}</p>
                ${booking.total_fare || booking.amount ? `<p class="price">₹${escapeHTML(String(booking.total_fare || booking.amount))}</p>` : ""}
            </div>
        `;
        return;
    }

    setContentHeading("Bus Booking", result.message);
    content.innerHTML = "";
}

function renderBusTrips(trips) {
    const content = document.getElementById("content-body");

    setContentHeading("Available Buses", `${trips.length} option(s) found`);

    content.innerHTML = `
        <div class="listing-grid">
            ${trips.map((trip, index) => `
                <div class="listing-card">
                    <div class="listing-card-body">
                        <h3>${escapeHTML(trip.bus_name || trip.operator || `Bus ${index + 1}`)}</h3>

                        <p>
                            ${escapeHTML(trip.origin || trip.from_city || "")}
                            →
                            ${escapeHTML(trip.destination || trip.to_city || "")}
                        </p>

                        <p>Departure: ${escapeHTML(trip.departure_time || "")}</p>
                        <p>Arrival: ${escapeHTML(trip.arrival_time || "")}</p>

                        <p>Available seats: ${escapeHTML(String(trip.available_seats ?? ""))}</p>

                        <p class="price">
                            ₹${escapeHTML(String(trip.fare ?? trip.price ?? ""))}
                        </p>
                    </div>
                </div>
            `).join("")}
        </div>
    `;
}

function renderBusSeats(seatMap) {
    const content = document.getElementById("content-body");

    if (!seatMap || !seatMap.decks) {
        content.innerHTML = "<p>Seat information is unavailable.</p>";
        return;
    }

    setContentHeading("Bus Seat Selection", "Seat availability");

    content.innerHTML = `
        <div class="seat-map">
            ${seatMap.decks.map(deck => `
                <div class="seat-deck">
                    <h3>${escapeHTML(deck.deck)} Deck</h3>

                    <div class="seat-grid">
                        ${deck.seats.map(seat => `
                            <button
                                class="seat ${seat.status.toLowerCase()}"
                                ${seat.status !== "AVAILABLE" ? "disabled" : ""}
                                title="${escapeHTML(seat.status)}"
                            >
                                ${escapeHTML(seat.seat_code)}
                            </button>
                        `).join("")}
                    </div>
                </div>
            `).join("")}
        </div>
    `;
}

function renderBusBookings(bookings) {
    const content = document.getElementById("content-body");

    setContentHeading("My Bus Bookings", `${bookings.length} booking(s)`);

    content.innerHTML = `
        <div class="listing-grid">
            ${bookings.map(booking => {
                const seats = Array.isArray(booking.seat_codes)
                    ? booking.seat_codes.join(", ")
                    : (booking.seat_codes || booking.seats || "-");

                return `
                <div class="listing-card">
                    <div class="listing-card-body">
                        <h3>${escapeHTML(booking.booking_code || booking.booking_id || booking.ticket_id || "Bus Booking")}</h3>
                        <p>Status: <span class="status-label status-available">${escapeHTML(booking.status || "CONFIRMED")}</span></p>
                        <p>Seats: ${escapeHTML(seats)}</p>
                        ${booking.total_fare || booking.amount ? `<p class="price">₹${escapeHTML(String(booking.total_fare || booking.amount))}</p>` : ""}
                    </div>
                </div>
                `;
            }).join("")}
        </div>
    `;
}

function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    })[char]);
}
