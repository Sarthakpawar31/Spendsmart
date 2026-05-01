const chartPalette = ["#0056D2", "#4C84FF", "#38A169", "#F6AD55", "#E53E3E", "#1A202C", "#7AA8FF", "#9F7AEA", "#48BB78"];

function parseJsonScript(id) {
  const node = document.getElementById(id);
  if (!node) return null;
  try {
    return JSON.parse(node.textContent);
  } catch (error) {
    return null;
  }
}

function createDoughnutChart(canvasId, labels, values) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  new Chart(canvas, {
    type: "doughnut",
    data: {
      labels,
      datasets: [{ data: values, backgroundColor: chartPalette, borderWidth: 0 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "68%",
      plugins: {
        legend: {
          position: "bottom",
          labels: { usePointStyle: true, color: "#1A202C", padding: 18 },
        },
      },
    },
  });
}

function createGaugeChart(canvasId, usage) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const safeUsage = Math.max(0, Math.min(usage, 100));
  const remaining = Math.max(100 - safeUsage, 0);
  const gaugeText = {
    id: "gaugeText",
    afterDraw(chart) {
      const { ctx, chartArea } = chart;
      if (!chartArea) return;
      ctx.save();
      ctx.font = "700 28px Inter";
      ctx.fillStyle = "#1A202C";
      ctx.textAlign = "center";
      ctx.fillText(`${safeUsage}%`, chart.width / 2, chartArea.top + 115);
      ctx.font = "500 13px Inter";
      ctx.fillStyle = "#718096";
      ctx.fillText("Budget used", chart.width / 2, chartArea.top + 140);
      ctx.restore();
    },
  };
  new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: ["Used", "Remaining"],
      datasets: [
        {
          data: [safeUsage, remaining],
          backgroundColor: [safeUsage >= 80 ? "#E53E3E" : safeUsage >= 50 ? "#F6AD55" : "#0056D2", "#E2E8F0"],
          borderWidth: 0,
          circumference: 180,
          rotation: 270,
          cutout: "72%",
          borderRadius: 8,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
    },
    plugins: [gaugeText],
  });
}

function createLineChart(canvasId, labels, values) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "Daily Spending",
        data: values,
        borderColor: "#0056D2",
        backgroundColor: "rgba(0, 86, 210, 0.14)",
        fill: true,
        tension: 0.35,
        pointRadius: 3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#718096" }, grid: { display: false } },
        y: { ticks: { color: "#718096" }, grid: { color: "#EDF2F7" } },
      },
    },
  });
}

function createBarChart(canvasId, labels, values) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Monthly Spending",
        data: values,
        backgroundColor: ["#4C84FF", "#0056D2", "#38A169", "#F6AD55", "#1A202C", "#7AA8FF"],
        borderRadius: 12,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#718096" }, grid: { display: false } },
        y: { ticks: { color: "#718096" }, grid: { color: "#EDF2F7" } },
      },
    },
  });
}

document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) {
    window.lucide.createIcons();
  }

  const menuToggle = document.getElementById("menuToggle");
  const sidebar = document.getElementById("sidebar");
  if (menuToggle && sidebar) {
    menuToggle.addEventListener("click", () => sidebar.classList.toggle("open"));
  }

  const userTypeSelect = document.getElementById("userTypeSelect");
  const studentFields = document.getElementById("studentFields");
  if (userTypeSelect && studentFields) {
    const syncStudentFields = () => {
      studentFields.classList.toggle("hidden", userTypeSelect.value !== "Student");
    };
    syncStudentFields();
    userTypeSelect.addEventListener("change", syncStudentFields);
  }

  const dashboardData = parseJsonScript("dashboard-chart-data");
  if (dashboardData) {
    createGaugeChart("budgetGaugeChart", dashboardData.usage || 0);
    createDoughnutChart("dashboardCategoryChart", dashboardData.categoryLabels || ["No data"], dashboardData.categoryValues || [1]);
  }

  const analyticsData = parseJsonScript("analytics-chart-data");
  if (analyticsData) {
    createDoughnutChart("analyticsDonutChart", analyticsData.categoryLabels || ["No data"], analyticsData.categoryValues || [1]);
    createLineChart("analyticsLineChart", analyticsData.dailyLabels || ["No data"], analyticsData.dailyValues || [0]);
    createBarChart("analyticsBarChart", analyticsData.monthlyLabels || ["No data"], analyticsData.monthlyValues || [0]);
  }
});
