function setContentHeading(title, subtitle = "") {
    document.getElementById("content-title").textContent = title;
    document.getElementById("content-subtitle").textContent = subtitle;
}

function renderResponse(result) {
    const intent = result.intent || "";

    // Payment is a separate frontend workflow.
    const payment = result.payment || result.data?.payment;
    const isPaymentAction = result.action === "open_payment" || result.stage === "payment_pending";
    const hasLockedSeatsUnpaid = payment && (!result.data?.booking?.booking_code && (result.data?.booking?.seat_labels || payment.seat_labels || payment.seat_codes || payment.seat_numbers));

    if (payment && (isPaymentAction || hasLockedSeatsUnpaid)) {
        renderPayment(payment, result);
        return;
    }

    // 1) The backend labels every reply with the service that produced it.
    //    This is the reliable way: `intent` is null once a flow finishes.
    switch (result.domain) {
        case "movie":
            renderMovieResponse(result);
            return;
        case "bus":
            renderBusResponse(result);
            return;
        case "flight":
            renderFlightResponse(result);
            return;
    }

    // 2) Legacy fallback for services that do not send `domain` yet (e.g. flights).
    //    Intent names overlap between services, so add `domain` to those agents too.
    if (isMovieIntent(intent)) {
        renderMovieResponse(result);
        return;
    }

    if (isBusIntent(intent)) {
        renderBusResponse(result);
        return;
    }

    if (isFlightIntent(intent)) {
        renderFlightResponse(result);
        return;
    }

    // 3) Unknown reply (greeting, plain follow-up question...): keep whatever the panel
    //    is already showing instead of wiping it. The empty state already lives in the HTML.
}

function isMovieIntent(intent) {
    return [
        "search_movies",
        "show_movies",
        "show_shows",
        "show_showtimes",
        "show_seats",
        "book_movie",
        "movie_info",
        "my_bookings",
        "cancel_booking"
    ].includes(intent);
}

function isBusIntent(intent) {
    return [
        "search_buses",
        "show_bus_trips",
        "show_bus_seats",
        "book_bus",
        "my_bus_bookings",
        "cancel_bus_booking",
        "bus_info"
    ].includes(intent);
}

function isFlightIntent(intent) {
    return [
        "search",
        "details",
        "seats",
        "book",
        "cancel",
        "my_bookings"
    ].includes(intent);
}