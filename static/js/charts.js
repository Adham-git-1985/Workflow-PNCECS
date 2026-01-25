document.addEventListener("DOMContentLoaded", function () {

    const chartCanvas = document.getElementById("statusChart");
    if (!chartCanvas) return;

    // منع إعادة الرسم عند الرجوع للصفحة
    if (chartCanvas.dataset.rendered === "1") return;
    chartCanvas.dataset.rendered = "1";

    const ctx = chartCanvas.getContext("2d");

    new Chart(ctx, {
        type: "pie",
        data: {
            labels: ["Approved", "Rejected", "In Progress"],
            datasets: [{
                data: [
                    chartCanvas.dataset.approved,
                    chartCanvas.dataset.rejected,
                    chartCanvas.dataset.inProgress
                ],
                backgroundColor: [
                    "#28a745",
                    "#dc3545",
                    "#ffc107"
                ]
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false   // 🔴 مهم: يمنع repaint ثقيل
        }
    });
});
