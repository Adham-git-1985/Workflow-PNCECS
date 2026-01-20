document.addEventListener("DOMContentLoaded", function () {

    const chartCanvas = document.getElementById("statusChart");
    if (!chartCanvas) return;

    const approved = parseInt(chartCanvas.dataset.approved);
    const rejected = parseInt(chartCanvas.dataset.rejected);
    const inProgress = parseInt(chartCanvas.dataset.inProgress);

    new Chart(chartCanvas, {
        type: 'pie',
        data: {
            labels: ['Approved', 'Rejected', 'In Progress'],
            datasets: [{
                data: [approved, rejected, inProgress],
                borderWidth: 1
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom'
                }
            }
        }
    });

});
