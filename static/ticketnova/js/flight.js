
function renderFlightResponse(result) {
    const content = document.getElementById("content-body");
    if (!content) return;

    const intent = result.intent || "";
    const data = result.data || {};
    const options = result.options || [];

    // 1. Payment workflow
    if (result.action === "open_payment" && (result.payment || data.payment)) {
        renderPayment(result.payment || data.payment, result);
        return;
    }

    // 2. Flight listings (options containing flights)
    const isFlightList = Array.isArray(options) && options.length > 0 && (
        Boolean(options[0]?.route) || Boolean(options[0]?.flight_date) || Boolean(options[0]?.seats_remaining) || Boolean(options[0]?.base_fare)
    );
    const dataHasFlights = Array.isArray(data.flights) && data.flights.length > 0;

    if (isFlightList || dataHasFlights || (intent === "search" && Array.isArray(options) && options.length > 0)) {
        renderFlightOptions(isFlightList ? options : data.flights);
        return;
    }

    // 3. Seat selection (options or data containing seats)
    const isSeatList = Array.isArray(options) && options.length > 0 && Boolean(options[0]?.seat_number);
    const dataHasSeats = Array.isArray(data.seats) && data.seats.length > 0;

    if (isSeatList || dataHasSeats || intent === "seats") {
        renderFlightSeats(isSeatList ? options : (data.seats || options), data);
        return;
    }

    // 4. Flight details (single flight object)
    const isFlightDetail = Boolean(data.route) || Boolean(data.flight?.route);
    if (intent === "details" || isFlightDetail) {
        renderFlightDetails(data.flight || data);
        return;
    }

    // 5. My bookings (list of bookings)
    const isBookingList = Array.isArray(options) && options.length > 0 && Boolean(options[0]?.pnr);
    const dataHasBookings = Array.isArray(data.bookings) && data.bookings.length > 0;
    if (intent === "my_bookings" || isBookingList || dataHasBookings) {
        renderFlightBookings(isBookingList ? options : (data.bookings || options));
        return;
    }

    // 6. Cancellation response or confirmation
    if (intent === "cancel" || data.pnr || data.refund) {
        renderFlightCancellation(data, result);
        return;
    }

    // 7. Booking progress / Passenger details / Locked seats summary
    if (data.seat_numbers || data.passengers || (intent === "book" && (data.flight_id || data.flight_number))) {
        renderFlightBookingProgress(data, result);
        return;
    }

    // 8. Search in progress (asking for missing fields: origin, destination, date, etc.)
    if (result.required_fields && result.required_fields.length > 0) {
        renderFlightSearchProgress(data, result);
        return;
    }

    // 9. Informational message
    setContentHeading("Flight Booking", result.message || "");
    if (result.message) {
        content.innerHTML = `
            <div class="booking-summary">
                <h3>Flight Booking</h3>
                <p>${escapeHTML(result.message)}</p>
            </div>
        `;
    } else {
        content.innerHTML = "";
    }
}

function renderFlightOptions(flights) {
    const content = document.getElementById("content-body");

    setContentHeading("Available Flights", `${flights.length} flight(s) found`);

    content.innerHTML = `
        <div class="listing-grid">
            ${flights.map(flight => {
                const route = flight.route || {};
                const airline = route.airline || "Airline";
                const flightNum = route.flight_number || "";
                const origin = route.origin_iata || "";
                const dest = route.destination_iata || "";
                const dep = route.departure_time || "";
                const arr = route.arrival_time || "";
                const date = flight.flight_date || "";
                const seats = flight.seats_remaining ?? "-";
                const fare = flight.base_fare ?? "-";
                const cabin = flight.cabin_class || "Economy";

                return `
                    <div class="listing-card">
                        <div class="listing-card-body">
                            <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                                <h3>${escapeHTML(airline)}</h3>
                                <span class="status-label status-available" style="font-size: 11px;">${escapeHTML(cabin)}</span>
                            </div>

                            <p style="font-weight: 600; color: #252b40;">${escapeHTML(flightNum)}</p>

                            <p style="font-size: 15px; font-weight: bold; color: #6658e8; margin: 8px 0;">
                                ${escapeHTML(origin)} → ${escapeHTML(dest)}
                            </p>

                            <p>Departure: <strong>${escapeHTML(dep)}</strong> | Arrival: <strong>${escapeHTML(arr)}</strong></p>
                            <p>Date: ${escapeHTML(date)}</p>
                            <p>Available seats: <strong>${escapeHTML(String(seats))}</strong></p>

                            <p class="price">
                                ₹${escapeHTML(String(fare))}
                            </p>
                        </div>
                    </div>
                `;
            }).join("")}
        </div>
    `;
}

function renderFlightDetails(flight) {
    const content = document.getElementById("content-body");

    setContentHeading("Flight Details");

    const route = flight.route || {};

    content.innerHTML = `
        <div class="booking-summary">
            <h3>${escapeHTML(route.airline || "Flight Details")}</h3>
            <p><strong>Flight Number:</strong> ${escapeHTML(route.flight_number || "-")}</p>
            <p><strong>Route:</strong> ${escapeHTML(route.origin_iata || "")} → ${escapeHTML(route.destination_iata || "")}</p>
            <p><strong>Date:</strong> ${escapeHTML(flight.flight_date || "-")}</p>
            <p><strong>Timings:</strong> ${escapeHTML(route.departure_time || "")} - ${escapeHTML(route.arrival_time || "")}</p>
            <p><strong>Cabin Class:</strong> ${escapeHTML(flight.cabin_class || "Economy")}</p>
            <p><strong>Seats remaining:</strong> ${escapeHTML(String(flight.seats_remaining ?? "-"))}</p>
            <p class="price">₹${escapeHTML(String(flight.base_fare ?? "-"))}</p>
        </div>
    `;
}

function renderFlightSeats(seats, data = {}) {
    const content = document.getElementById("content-body");

    setContentHeading("Flight Seat Selection", "Select your preferred seat(s)");

    const flightNumber = data.flight_number || "";
    const passengerCount = data.passenger_count || 1;

    content.innerHTML = `
        <div class="seat-map">
            <div class="booking-summary" style="margin-top: 0;">
                <h3>Flight Seat Map</h3>
                ${flightNumber ? `<p><strong>Flight:</strong> ${escapeHTML(flightNumber)}</p>` : ""}
                <p><strong>Passengers:</strong> ${escapeHTML(String(passengerCount))}</p>
                <p style="font-size: 12px; color: #777f91;">Please select ${escapeHTML(String(passengerCount))} seat(s) by replying in the chat.</p>
            </div>

            <div class="seat-deck">
                <div class="screen-display" style="border-top-color: #6658e8;">
                    ✈️ COCKPIT THIS WAY
                </div>

                <div class="seat-grid" style="grid-template-columns: repeat(6, minmax(36px, 46px)); justify-content: center;">
                    ${seats.map(seat => `
                        <button
                            type="button"
                            class="seat ${seat.status ? seat.status.toLowerCase() : "available"}"
                            ${seat.status !== "AVAILABLE" ? "disabled" : ""}
                            title="${escapeHTML(seat.seat_number)} · ${escapeHTML(seat.seat_type || "")} · ${escapeHTML(seat.status || "")}"
                        >
                            ${escapeHTML(seat.seat_number)}
                        </button>
                    `).join("")}
                </div>

                <div class="seat-legend" style="margin-top: 18px;">
                    <span><i class="available"></i> Available</span>
                    <span><i class="locked"></i> Locked</span>
                    <span><i class="booked"></i> Booked</span>
                </div>
            </div>
        </div>
    `;
}

function renderFlightBookings(bookings) {
    const content = document.getElementById("content-body");

    setContentHeading("My Flight Bookings", `${bookings.length} booking(s) found`);

    if (!bookings.length) {
        content.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">✈️</div>
                <h2>No flight bookings yet</h2>
                <p>You haven't made any flight bookings yet.</p>
            </div>
        `;
        return;
    }

    content.innerHTML = `
        <div class="listing-grid">
            ${bookings.map(booking => {
                const seats = Array.isArray(booking.seat_numbers)
                    ? booking.seat_numbers.join(", ")
                    : String(booking.seat_numbers || "-");

                return `
                    <div class="listing-card">
                        <div class="listing-card-body">
                            <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                                <h3>PNR: ${escapeHTML(booking.pnr || "Confirmed")}</h3>
                                <span class="status-label ${booking.status === "CONFIRMED" ? "status-available" : "status-booked"}">
                                    ${escapeHTML(booking.status || "CONFIRMED")}
                                </span>
                            </div>

                            ${booking.flight?.flight_number ? `<p>Flight: <strong>${escapeHTML(booking.flight.flight_number)}</strong></p>` : ""}
                            ${booking.flight?.flight_date ? `<p>Date: ${escapeHTML(booking.flight.flight_date)}</p>` : ""}
                            <p>Seats: <strong>${escapeHTML(seats)}</strong></p>

                            <p class="price">₹${escapeHTML(String(booking.total_fare ?? ""))}</p>
                        </div>
                    </div>
                `;
            }).join("")}
        </div>
    `;
}

function renderFlightBookingProgress(data, result) {
    const content = document.getElementById("content-body");

    setContentHeading("Flight Booking in Progress", result.message || "");

    const seats = Array.isArray(data.seat_numbers)
        ? data.seat_numbers.join(", ")
        : String(data.seat_numbers || "-");

    const passengers = Array.isArray(data.passengers) ? data.passengers : [];

    content.innerHTML = `
        <div class="booking-summary">
            <h3>Flight Booking Summary</h3>
            ${data.flight_number ? `<p><strong>Flight:</strong> ${escapeHTML(data.flight_number)}</p>` : ""}
            ${data.flight_id ? `<p><strong>Flight ID:</strong> ${escapeHTML(String(data.flight_id))}</p>` : ""}
            ${data.seat_numbers ? `<p><strong>Selected Seats:</strong> <span class="status-label status-locked">${escapeHTML(seats)}</span></p>` : ""}
            ${data.passenger_count ? `<p><strong>Passenger Count:</strong> ${escapeHTML(String(data.passenger_count))}</p>` : ""}

            ${passengers.length > 0 ? `
                <div style="margin-top: 14px; border-top: 1px solid #f0f2f7; padding-top: 10px;">
                    <strong style="font-size: 13px; color: #252b40;">Passenger Details:</strong>
                    <ul style="margin: 8px 0 0 18px; padding: 0; font-size: 13px; color: #636b7e;">
                        ${passengers.map(p => `
                            <li>${escapeHTML(p.name || p.full_name || "Passenger")} (${escapeHTML(String(p.age || "-"))} yrs, ${escapeHTML(p.gender || "-")})</li>
                        `).join("")}
                    </ul>
                </div>
            ` : ""}

            <p style="font-size: 13px; color: #777f91; margin-top: 14px;">
                ${escapeHTML(result.message || "")}
            </p>
        </div>
    `;
}

function renderFlightCancellation(data, result) {
    const content = document.getElementById("content-body");

    setContentHeading("Flight Cancellation", result.message || "");

    const pnr = data.pnr || "";
    const refund = data.refund || {};

    content.innerHTML = `
        <div class="booking-summary" style="border-left: 5px solid #c34b4b;">
            <h3 style="color: #c34b4b;">Cancellation Status</h3>
            ${pnr ? `<p><strong>PNR:</strong> <span style="font-family: monospace;">${escapeHTML(pnr)}</span></p>` : ""}
            ${refund.amount ? `<p><strong>Refund Amount:</strong> ₹${escapeHTML(String(refund.amount))}</p>` : ""}
            ${refund.status ? `<p><strong>Refund Status:</strong> ${escapeHTML(refund.status)}</p>` : ""}
            <p style="font-size: 13px; color: #777f91; margin-top: 10px;">
                ${escapeHTML(result.message || "Cancellation processed.")}
            </p>
        </div>
    `;
}

function renderFlightSearchProgress(data, result) {
    const content = document.getElementById("content-body");

    setContentHeading("Flight Search", result.message || "");

    const filters = data.search_filters || data || {};
    const origin = filters.origin_iata || "—";
    const dest = filters.destination_iata || "—";
    const date = filters.flight_date || "—";
    const passengers = filters.passenger_count || "1";
    const cabin = filters.cabin_class || "Economy";

    content.innerHTML = `
        <div class="booking-summary">
            <h3>Flight Search Criteria</h3>
            <div style="display: flex; gap: 15px; margin: 12px 0; font-size: 16px; font-weight: bold; color: #6658e8;">
                <span>${escapeHTML(origin)}</span>
                <span>✈️ →</span>
                <span>${escapeHTML(dest)}</span>
            </div>
            <p><strong>Travel Date:</strong> ${escapeHTML(date)}</p>
            <p><strong>Passengers:</strong> ${escapeHTML(String(passengers))}</p>
            <p><strong>Cabin Class:</strong> ${escapeHTML(cabin)}</p>
            <p style="font-size: 13px; color: #777f91; margin-top: 12px;">
                ${escapeHTML(result.message || "")}
            </p>
        </div>
    `;
}
