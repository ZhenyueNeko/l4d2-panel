/* 主题与侧边栏状态管理（亮色主题、展开侧栏为默认；用户手动切换后记忆优先）。
   本脚本需在 <head> 中、style.css 之前加载，避免主题闪烁。 */
(function () {
    // 主题：读取记忆；未记忆过时缺省亮色（只有明确切到过深色才用深色）
    var storedTheme = null;
    try {
        storedTheme = localStorage.getItem('l4d2-theme');
    } catch (e) { /* 隐私模式下 localStorage 不可用，忽略 */ }
    document.documentElement.setAttribute('data-theme', storedTheme === 'dark' ? 'dark' : 'light');

    // 侧边栏：读取记忆；未记忆过时缺省展开（只有明确收起过才用窄图标条）
    try {
        if (localStorage.getItem('l4d2-sidebar') !== 'narrow') {
            document.documentElement.setAttribute('data-sidebar', 'expanded');
        }
    } catch (e) { /* 同上，忽略 */ }

    function syncSidebarButton() {
        var btn = document.getElementById('sidebarToggle');
        if (btn) {
            var expanded = document.documentElement.getAttribute('data-sidebar') === 'expanded';
            btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', syncSidebarButton);
    } else {
        syncSidebarButton();
    }

    document.addEventListener('click', function (e) {
        var themeBtn = e.target.closest('#themeToggle');
        if (themeBtn) {
            var nextTheme = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-theme', nextTheme);
            try {
                localStorage.setItem('l4d2-theme', nextTheme);
            } catch (err) { /* 忽略 */ }
            return;
        }

        var sideBtn = e.target.closest('#sidebarToggle');
        if (sideBtn) {
            var expanded = document.documentElement.getAttribute('data-sidebar') === 'expanded';
            if (expanded) {
                document.documentElement.removeAttribute('data-sidebar');
            } else {
                document.documentElement.setAttribute('data-sidebar', 'expanded');
            }
            try {
                localStorage.setItem('l4d2-sidebar', expanded ? 'narrow' : 'expanded');
            } catch (err) { /* 忽略 */ }
            syncSidebarButton();
        }
    });
})();
