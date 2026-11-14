

function renderMovieResponse(result) {
    const content = document.getElementById("content-body");
    const data = result.data || {};
    const options = result.options || [];

    setContentHeading("Movie Booking", result.message || "");

    // 1. Movie listings
    if (Array.isArray(data.movies)) {
        renderMovieOptions(data.movies);
        return;
    }

    // 2. Show listings
    if (Array.isArray(data.shows)) {
        renderMovieShows(data.shows);
        return;
    }

    // 3. Booking details
    if (data.booking) {
        renderMovieBooking(data.booking);
        return;
    }

    // 4. Seat map
    if (data.seat_map) {
        renderMovieSeatMap(data.seat_map);
        return;
    }

    // 5. Movie information
    if (data.movie) {
        renderMovieOptions([data.movie]);
        return;
    }

    // 6. Other choices, such as theater and show-time options
    if (options.length > 0) {
        renderMovieChoices(options);
        return;
    }

    // 7. Nothing to display
    content.innerHTML = "";
}


function renderMovieOptions(movies) {
    const content = document.getElementById("content-body");

    content.innerHTML = `
        <div class="listing-grid">
            ${movies.map(movie => `
                <div class="listing-card">
                    ${movie.poster ? `
                        <img class="poster"
                             src="${escapeHTML(movie.poster)}"
                             alt="${escapeHTML(movie.title || "Movie poster")}"
                             onerror="this.style.display='none'">
                    ` : ""}

                    <div class="listing-card-body">
                        <h3>${escapeHTML(movie.title || "Movie")}</h3>
                        <p>Language: ${escapeHTML(movie.language || "-")}</p>
                        <p>Genre: ${escapeHTML(movie.genre || "-")}</p>
                        <p>Duration: ${escapeHTML(String(movie.duration_mins ?? "-"))} minutes</p>
                        <p>Certification: ${escapeHTML(movie.certification || "-")}</p>
                    </div>
                </div>
            `).join("")}
        </div>
    `;
}

function renderMovieShows(shows) {
    const content = document.getElementById("content-body");

    content.innerHTML = `
        <div class="listing-grid">
            ${shows.map(show => `
                <div class="listing-card">
                    <div class="listing-card-body">
                        <h3>${escapeHTML(show.theater || "Theater")}</h3>
                        <p>Screen: ${escapeHTML(show.screen || "-")}</p>
                        <p>Date: ${escapeHTML(show.show_date || "-")}</p>
                        <p>Time: ${escapeHTML(show.start_time || "-")}</p>
                        <p>Price: ₹${escapeHTML(String(show.base_price ?? "-"))}</p>
                        <p>Available seats: ${escapeHTML(String(show.available_seats ?? "-"))}</p>
                    </div>
                </div>
            `).join("")}
        </div>
    `;
}

function renderMovieChoices(options) {
    const content = document.getElementById("content-body");

    content.innerHTML = `
        <div class="listing-grid">
            ${options.map(option => `
                <div class="listing-card">
                    <div class="listing-card-body">
                        <h3>${escapeHTML(option.label || option.value || "Option")}</h3>
                    </div>
                </div>
            `).join("")}
        </div>
    `;
}


function renderMovieBooking(booking) {
    const content = document.getElementById("content-body");

    const seats = Array.isArray(booking.seat_labels)
        ? booking.seat_labels.join(", ")
        : (booking.seat_labels || "");

    const movieName = booking.show?.movie || booking.show?.movie_title || booking.movie || "";
    const totalAmount = booking.amount_in_rupees || booking.total_amount || "";

    content.innerHTML = `
        <div class="booking-summary">
            <h3>Movie Booking Details</h3>
            ${booking.booking_code ? `<p><strong>Booking code:</strong> ${escapeHTML(booking.booking_code)}</p>` : ""}
            <p><strong>Status:</strong> <span class="status-label ${booking.status === "CONFIRMED" ? "status-available" : "status-locked"}">${escapeHTML(booking.status || "CONFIRMED")}</span></p>
            ${movieName ? `<p><strong>Movie:</strong> ${escapeHTML(movieName)}</p>` : ""}
            ${booking.show?.theater ? `<p><strong>Theater:</strong> ${escapeHTML(booking.show.theater)}</p>` : ""}
            ${booking.show?.screen ? `<p><strong>Screen:</strong> ${escapeHTML(booking.show.screen)}</p>` : ""}
            ${seats ? `<p><strong>Seats:</strong> ${escapeHTML(seats)}</p>` : ""}
            ${totalAmount ? `<p class="price">₹${escapeHTML(String(totalAmount))}</p>` : ""}
        </div>
    `;
}


function renderMovieSeatMap(seatMap) {
    const content = document.getElementById("content-body");

    const show = seatMap.show || {};
    const summary = seatMap.summary || {};
    const rows = seatMap.rows || [];

    content.innerHTML = `
        <div class="seat-map">

            <div class="booking-summary">
                <h3>Movie Seat Selection</h3>
                <p>Movie: ${escapeHTML(show.movie || "")}</p>
                <p>Theater: ${escapeHTML(show.theater || "")}</p>
                <p>Screen: ${escapeHTML(show.screen || "")}</p>
                <p>Date: ${escapeHTML(show.show_date || "")}</p>
                <p>Time: ${escapeHTML(show.start_time || "")}</p>
                <p>Available: ${summary.AVAILABLE ?? 0} / ${summary.total ?? 0}</p>
            </div>

            <div class="seat-deck">
                <h3>SCREEN</h3>

                <div class="screen-display">
                    SCREEN THIS WAY
                </div>

                ${rows.map(row => `
                    <div class="seat-row">
                        <span class="row-label">${escapeHTML(row.row)}</span>

                        <div class="seat-grid">
                            ${row.seats.map(seat => `
                                <button
                                    type="button"
                                    class="seat ${seat.status.toLowerCase()}"
                                    data-seat="${escapeHTML(seat.seat_label)}"
                                    ${seat.status !== "AVAILABLE" ? "disabled" : ""}
                                    title="${escapeHTML(seat.seat_label)} | ₹${escapeHTML(seat.price)} | ${escapeHTML(seat.tier)}"
                                >
                                    ${escapeHTML(seat.seat_label)}
                                </button>
                            `).join("")}
                        </div>
                    </div>
                `).join("")}

            </div>

            <div class="seat-legend">
                <span><i class="available"></i> Available</span>
                <span><i class="locked"></i> Locked</span>
                <span><i class="booked"></i> Booked</span>
            </div>

        </div>
    `;
}

