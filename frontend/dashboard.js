const money = new Intl.NumberFormat('en-IN', {style:'currency',currency:'INR',maximumFractionDigits:2});
function updateClock() {
  document.getElementById('clock').textContent = new Date().toLocaleString('en-IN', {weekday:'short',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
}
async function refreshDashboard() {
  try {
    const response = await fetch('/api/dashboard', {cache:'no-store'});
    if (!response.ok) throw new Error();
    const data = await response.json();
    for(const key of ['open_tables','open_takeouts','settled_bills']) document.getElementById(key).textContent = data[key];
    document.getElementById('sales').textContent = money.format(data.sales);
    document.getElementById('status').textContent = '';
  } catch {
    document.getElementById('status').textContent = 'Live totals are unavailable. Check that the POS server is running.';
  }
}
fetch('/api/config').then(r=>r.json()).then(c=>{
  document.getElementById('business-name').textContent = c.restaurant_name;
  document.getElementById('takeout-action').hidden = !c.enable_takeout;
}).catch(()=>{});
updateClock(); refreshDashboard();
setInterval(updateClock,30000); setInterval(refreshDashboard,15000);
