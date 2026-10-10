// 共享文本编辑器模态（配套 templates/editor_modal.html）：
//   - 文件管理页：编辑服务器侧文件（load/save 注入 /api/files 端点）
//   - 插件管理页：编辑库内插件配置/依赖文件（load/save 注入 /api/plugins/config 端点）
// 用法：EditorModal.open({
//   title,                       // 模态标题
//   files: ['a.txt', ...],       // 可选：多文件时的切换列表（单文件可省略）
//   current: 'a.txt',            // 初始选中文件（files 存在时）
//   hint: '<b>...</b>',          // 可选：编辑区上方的说明（支持 HTML）
//   load: async (file) => text,  // 读取文件内容
//   save: async (file, content), // 保存内容，可返回 { note } 作为保存提示的补充说明
//   onSaved: (file) => {}        // 可选：保存成功后的回调
// })
const EditorModal = (() => {
    let opts = null;
    let currentFile = null;
    let saving = false;
    let indentUnit = '\t';   // 缩进单位，可由 open({ indentUnit }) 覆盖，默认 Tab

    function close() {
        document.getElementById('editorModal').style.display = 'none';
    }

    // --- 编辑区按键行为：Tab 缩进 / Shift+Tab 反缩进 / Ctrl+S 保存 ---
    // Tab 的浏览器默认行为是把焦点移出弹窗（textarea → 按钮 → 弹窗背后的页面），
    // 编辑文件时根本无法缩进；这里按代码编辑器惯例拦截改写。插入文本用
    // execCommand('insertText') 而非手动拼 value：它是唯一保留浏览器原生撤销栈
    // （Ctrl+Z 可撤销）的方式，不支持的旧浏览器退回 setRangeText（撤销会断）。
    function replaceSelection(textarea, text) {
        if (document.execCommand && document.execCommand('insertText', false, text)) {
            return;
        }
        textarea.setRangeText(text, textarea.selectionStart, textarea.selectionEnd, 'end');
    }

    function indentSelection(textarea, outdent) {
        const value = textarea.value;
        const start = textarea.selectionStart;
        const end = textarea.selectionEnd;
        if (start === end && !outdent) {
            replaceSelection(textarea, indentUnit);   // 空选区：光标处插入一个缩进单位
            return;
        }
        // 有选区：展开到整行边界，对选中的每一行整体加/删一级缩进
        const lineStart = value.lastIndexOf('\n', start - 1) + 1;
        let lineEnd = value.indexOf('\n', end);
        if (lineEnd === -1) lineEnd = value.length;
        const lines = value.slice(lineStart, lineEnd).split('\n');
        const updated = lines.map(line => {
            if (outdent) {
                return line.startsWith(indentUnit) ? line.slice(indentUnit.length) : line;
            }
            return indentUnit + line;
        }).join('\n');
        textarea.setSelectionRange(lineStart, lineEnd);
        replaceSelection(textarea, updated);
        textarea.setSelectionRange(lineStart, lineStart + updated.length);   // 重新选中整块，连续操作顺手
    }

    function onTextareaKeydown(e) {
        if (e.isComposing) return;   // 中文输入法组词中的 Tab 放行
        if (e.key !== 'Tab' || e.ctrlKey || e.metaKey || e.altKey) return;
        e.preventDefault();
        indentSelection(e.target, e.shiftKey);
    }

    // Ctrl+S / Cmd+S 保存（仅在编辑弹窗可见时接管，避免劫持页面的浏览器保存）
    function onDocumentKeydown(e) {
        if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
        if (e.key.toLowerCase() !== 's') return;
        const modal = document.getElementById('editorModal');
        if (!modal || modal.style.display !== 'flex') return;
        e.preventDefault();
        if (e.isComposing) return;
        save();
    }

    let keysBound = false;
    function bindEditorKeys() {
        if (keysBound) return;
        keysBound = true;   // editor.js 在 head 加载时弹窗 DOM 尚未渲染，首次 open 再绑定
        document.getElementById('editorTextarea').addEventListener('keydown', onTextareaKeydown);
        document.addEventListener('keydown', onDocumentKeydown);
    }

    async function reload() {
        const textarea = document.getElementById('editorTextarea');
        textarea.value = '';
        textarea.placeholder = '正在加载文件内容...';
        try {
            textarea.value = await opts.load(currentFile);
        } catch (error) {
            textarea.value = `加载文件失败: ${error.message}`;
        }
    }

    function open(options) {
        opts = options;
        indentUnit = options.indentUnit || '\t';
        bindEditorKeys();
        const titleEl = document.getElementById('editorFileName');
        const select = document.getElementById('editorFileSelect');
        const pathBar = document.getElementById('editorFilePath');
        const hintEl = document.getElementById('editorHint');
        titleEl.textContent = options.title || '编辑文件';
        if (options.files && options.files.length > 1) {
            // 多文件：下拉切换器（选中项即当前文件路径）
            select.style.display = 'block';
            pathBar.style.display = 'none';
            select.innerHTML = options.files.map(f =>
                `<option value="${String(f).replace(/"/g, '&quot;')}">${String(f).replace(/&/g, '&amp;').replace(/</g, '&lt;')}</option>`).join('');
            currentFile = options.current || options.files[0];
            select.value = currentFile;
        } else if (options.files && options.files.length === 1) {
            // 单文件：路径以只读文本条显示，让用户明确编辑的是哪个目录下的文件
            select.style.display = 'none';
            pathBar.style.display = 'block';
            pathBar.textContent = options.files[0];
            currentFile = options.files[0];
        } else {
            select.style.display = 'none';
            select.innerHTML = '';
            pathBar.style.display = 'none';
            currentFile = options.current || null;
        }
        if (options.hint) {
            hintEl.style.display = 'block';
            hintEl.innerHTML = options.hint;
        } else {
            hintEl.style.display = 'none';
            hintEl.innerHTML = '';
        }
        document.getElementById('editorModal').style.display = 'flex';
        reload();
    }

    async function save() {
        if (saving) return;   // Ctrl+S 连按/保存进行中防重入
        saving = true;
        const content = document.getElementById('editorTextarea').value;
        let note = null;
        try {
            const result = await opts.save(currentFile, content);
            note = result && result.note;
        } catch (error) {
            saving = false;
            Swal.fire('失败!', `保存失败: ${error.message}`, 'error');
            return;
        }
        saving = false;
        close();
        Swal.fire('已保存!', note || '文件内容已成功保存。', 'success');
        if (opts.onSaved) opts.onSaved(currentFile);
    }

    function switchFile() {
        const select = document.getElementById('editorFileSelect');
        if (currentFile !== select.value) {
            currentFile = select.value;
            reload();
        }
    }

    return { open, close, save, switchFile };
})();
