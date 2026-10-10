// 插件"识别导入"确认弹窗（文件管理页与插件管理页共用）。
// 依赖页面已加载 SweetAlert2；用法：
//   PluginImportDialog.showConfirm({ importId, candidates, onImported })
// candidates 元素结构见 /api/plugins/recognize 返回：
//   { type: 'plugin', folder, name, smx_files, conflict_with?, batch_conflict?, name_invalid? }
// 识别唯一标准：文件夹直接包含 left4dead2 目录层即为一个插件（默认全选）；
// 搜索/改名/组级全选/确认按钮实时计数等交互保留。
(function () {
    'use strict';

    function escHtml(text) {
        return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    function escAttr(text) {
        return escHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // 与后端 _sanitize_plugin_import_name 同规则的非法名判定（兜底用正则实现）
    const ILLEGAL_NAME_RE = /[\\/:*?"<>|'`;&$\x00-\x1f\x7f]/;
    function nameIsIllegal(name) {
        return !name || !name.trim() || name.trim().length > 100 || ILLEGAL_NAME_RE.test(name);
    }

    // 每个候选项的解释说明（按当前名字动态计算，改名后即时变化）
    function detailNotes(c) {
        const notes = [];
        if (nameIsIllegal(c.name)) {
            notes.push('文件夹名含不合法字符（\\ / : * ? " < > | 等），无法直接导入——请点击名称修改为合法名称');
        }
        if ((c.conflict_with || []).length) {
            notes.push(`与已有插件（${escHtml(c.conflict_with.join('、'))}）含同名 .smx，疑似同一插件的其他版本，同时安装会互相覆盖`);
        }
        if (c.batch_conflict) {
            notes.push('与本包内其他候选项含同名 .smx（如"已启用合集"与逐项插件重复），同时导入会互相覆盖，请只勾选其一');
        }
        return notes;
    }

    function flagsHtml(c) {
        const notes = detailNotes(c);
        if (!notes.length) return '';
        return `<div class="import-flags">${notes.map(n => `<div class="import-flag warn">${n}</div>`).join('')}</div>`;
    }

    function rowHtml(c, i) {
        const notes = detailNotes(c);
        const badge = notes.length
            ? `<span class="import-badge" title="${escAttr(notes[notes.length - 1])}">⚠ 注意</span>`
            : '';
        return `
            <div class="import-row" data-idx="${i}">
                <div class="import-main">
                    <input type="checkbox" class="import-check"${c._checked ? ' checked' : ''}>
                    <span class="import-name-text" title="点击改名">${escHtml(c.name)}</span>
                    ${badge}
                </div>
                ${flagsHtml(c)}
            </div>`;
    }

    function showConfirm(options) {
        const importId = options.importId;
        const candidates = options.candidates || [];
        const onImported = options.onImported || function () {};

        // 行状态：勾选与名字（搜索过滤/改名/全选都在这份状态上操作）
        const rowState = candidates.map(() => ({ checked: true, name: '' }));
        candidates.forEach((c, i) => {
            c._checked = true;
            rowState[i] = { checked: true, name: c.name };
        });

        function checkedCount() {
            return rowState.filter(r => r.checked).length;
        }

        const summaryHtml = `
            <div class="import-summary">
                <span>可导入 <b>${candidates.length}</b></span>
            </div>`;
        const searchHtml = candidates.length > 20
            ? `<input type="text" class="swal2-input import-search" placeholder="搜索插件名（仅过滤显示，不影响勾选）...">`
            : '';

        const listHtml = `
            <div class="import-section">
                <div class="import-section-title">
                    <span class="t">✅ 可导入项（默认全部导入）</span>
                    <span class="sub">${candidates.length} 项</span>
                    <span class="import-section-actions">
                        <button type="button" class="import-mini" data-group="all" data-to="1">全选</button>
                        <button type="button" class="import-mini" data-group="all" data-to="0">全不选</button>
                    </span>
                </div>
                <div class="import-box large">
                    ${candidates.map((c, i) => rowHtml(c, i)).join('')}
                </div>
            </div>`;
        const junkHtml = `<div class="import-hint">与现有插件同名时自动追加序号。</div>`;

        Swal.fire({
            title: `识别到 ${candidates.length} 个可导入项`,
            html: `${summaryHtml}${searchHtml}${listHtml}${junkHtml}`,
            customClass: { popup: 'import-popup' },
            showCancelButton: true,
            confirmButtonText: `导入所选 ${checkedCount()} 项`,
            cancelButtonText: '取消',
            focusConfirm: false,
            preConfirm: () => {
                const picked = [];
                let emptyName = false;
                const illegalNames = [];
                candidates.forEach((c, i) => {
                    if (!rowState[i].checked) return;
                    const name = rowState[i].name.trim();
                    if (!name) { emptyName = true; return; }
                    if (nameIsIllegal(name)) { illegalNames.push(name); return; }
                    picked.push({ import_id: importId, folder: c.folder, name: name });
                });
                if (emptyName) { Swal.showValidationMessage('插件名称不能为空'); return false; }
                if (illegalNames.length) {
                    Swal.showValidationMessage('以下名称含不合法字符（\\ / : * ? " < > | 等），请点击名称修改后再导入：'
                        + escHtml(illegalNames.join('、')));
                    return false;
                }
                if (picked.length === 0) { Swal.showValidationMessage('请至少选择一个插件'); return false; }
                return picked;
            }
        }).then(async (result) => {
            if (result.isConfirmed) {
                await importSelected(result.value, onImported);
            } else if (options.onCancelled) {
                // 用户取消（按钮/Esc/点击遮罩）：交由调用方处理收尾（如移除刚上传的文件夹）
                options.onCancelled();
            }
        });

        bindPopupInteractions(candidates, rowState);
    }

    // 弹窗内交互：勾选计数、全选/全不选、搜索过滤、点击名字改名
    function bindPopupInteractions(candidates, rowState) {
        const popup = Swal.getPopup();
        if (!popup) return;

        const confirmBtn = Swal.getConfirmButton();
        function refreshConfirmText() {
            const n = rowState.filter(r => r.checked).length;
            confirmBtn.textContent = `导入所选 ${n} 项`;
        }

        popup.addEventListener('change', (e) => {
            if (!e.target.classList.contains('import-check')) return;
            const row = e.target.closest('.import-row');
            rowState[Number(row.dataset.idx)].checked = e.target.checked;
            candidates[Number(row.dataset.idx)]._checked = e.target.checked;
            refreshConfirmText();
        });

        popup.addEventListener('click', (e) => {
            const mini = e.target.closest('.import-mini');
            if (mini) {
                const to = mini.dataset.to === '1';
                rowState.forEach((r, i) => {
                    r.checked = to;
                    candidates[i]._checked = to;
                    const cb = popup.querySelector(`.import-row[data-idx="${i}"] .import-check`);
                    if (cb) cb.checked = to;
                });
                refreshConfirmText();
                return;
            }
            const nameText = e.target.closest('.import-name-text');
            if (nameText) beginRename(nameText);
        });

        popup.addEventListener('input', (e) => {
            if (!e.target.classList.contains('import-search')) return;
            const q = e.target.value.trim().toLowerCase();
            popup.querySelectorAll('.import-row').forEach(row => {
                const name = rowState[Number(row.dataset.idx)].name.toLowerCase();
                row.style.display = (!q || name.includes(q)) ? '' : 'none';
            });
        });

        function beginRename(nameText) {
            const row = nameText.closest('.import-row');
            const idx = Number(row.dataset.idx);
            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'swal2-input import-name-input';
            input.value = rowState[idx].name;
            nameText.replaceWith(input);
            input.focus();
            input.select();
            let done = false;
            const commit = () => {
                if (done) return;
                done = true;
                const value = input.value.trim();
                if (value) {
                    rowState[idx].name = value;
                    candidates[idx].name = value;
                }
                // 整行重绘（outerHTML 替换，避免嵌套行）：非法名提示/徽标随改名即时消失，
                // 勾选状态取自 c._checked；事件监听挂在弹窗容器上，替换后依然生效
                row.outerHTML = rowHtml(candidates[idx], idx);
            };
            input.addEventListener('blur', commit);
            input.addEventListener('keydown', (ev) => {
                if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
                if (ev.key === 'Escape') { done = true; input.blur(); }
            });
        }
    }

    async function importSelected(picked, onImported) {
        Swal.fire({ title: '正在导入插件...', allowOutsideClick: false, didOpen: () => Swal.showLoading() });
        let data;
        try {
            const response = await fetch('/api/plugins/import_archive', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ plugins: picked })
            });
            data = await response.json();
            if (!response.ok) throw new Error(data.message || `HTTP 错误! 状态: ${response.status}`);
        } catch (error) {
            Swal.fire('失败!', `导入失败: ${error.message}`, 'error');
            onImported();
            return;
        }
        const okResults = data.results.filter(r => r.success);
        const failResults = data.results.filter(r => !r.success);
        const renamed = okResults.filter(r => r.final_name && r.final_name !== r.name);
        const renameNote = renamed.length
            ? `<br><br>以下插件因同名自动追加序号：${renamed.map(r => escHtml(r.final_name)).join('、')}`
            : '';
        if (failResults.length === 0) {
            Swal.fire('导入成功!', `已导入 ${okResults.length} 个可安装插件。${renameNote}`, 'success');
        } else {
            Swal.fire({
                icon: 'warning',
                title: '部分插件导入失败',
                html: `成功 ${okResults.length} 个, 失败 ${failResults.length} 个。<br><br>
                       <b>错误详情:</b><br>
                       <pre style="text-align: left; max-height: 150px; overflow-y: auto;">${failResults.map(r =>
                            `${r.name || '(未命名)'}: ${r.message}`).join('\n')}</pre>${renameNote}`
            });
        }
        onImported();
    }

    window.PluginImportDialog = { showConfirm };
})();
