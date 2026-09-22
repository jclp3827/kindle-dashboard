// Kindle Dashboard —— 读取 macOS 提醒事项(JXA / JavaScript for Automation)
// 用法:osascript -l JavaScript read_reminders.js
// 输出:{updated_at, reminders:[{title, completed, list, dueDate, priority}], calendar_events:[]}
// 注:首次运行 macOS 会弹窗请求访问“提醒事项”,需点允许。
const app = Application("Reminders");
const lists = app.lists();
const reminders = [];

function localISOString(d) {
    if (!d) return null;
    const pad = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
           `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

for (const list of lists) {
    const items = list.reminders();
    for (const item of items) {
        const due = item.dueDate();
        reminders.push({
            title: item.name(),
            completed: item.completed(),
            list: list.name(),
            dueDate: localISOString(due),
            priority: item.priority()
        });
    }
}

JSON.stringify({
    updated_at: new Date().toISOString(),
    reminders: reminders,
    calendar_events: []
});
