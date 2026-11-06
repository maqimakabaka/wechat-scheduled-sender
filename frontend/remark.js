// 好友备注匹配独立于普通群发计划，只执行用户确认过的名单。
(() => {
  const account = $('accountSel');
  const keywords = $('remarkKeywords');
  const text = $('remarkText');
  const preview = $('remarkPreview');
  const selectAll = $('remarkSelectAll');
  const start = $('remarkStart');
  const list = $('remarkMatches');
  const count = $('remarkCount');
  let matches = [];
  let revision = 0;
  let busy = false;

  function selectedIds(){
    return Array.from(list.querySelectorAll('input:checked'), el => el.value);
  }

  function updateControls(){
    const selected = selectedIds().length;
    const eligible = matches.filter(m => m.eligible).length;
    preview.disabled = busy || !keywords.value.trim();
    selectAll.disabled = busy || !eligible;
    selectAll.textContent = eligible && selected === eligible ? '取消全选' : '全选可发送';
    start.disabled = busy || !selected || !text.value.trim();
    if(matches.length) count.textContent = `匹配 ${matches.length} 人，已选 ${selected} 人`;
  }

  function reset(message){
    revision += 1;
    matches = [];
    list.replaceChildren();
    list.hidden = true;
    count.textContent = message;
    updateControls();
  }

  function post(path, body){
    return api(path, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
  }

  function renderMatches(){
    const rows = document.createDocumentFragment();
    for(const match of matches){
      const row = document.createElement('label');
      row.className = 'msel-item';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = match.username;
      checkbox.disabled = !match.eligible;
      const label = document.createElement('span');
      label.className = 'nm';
      const identity = match.alias || match.username;
      const nickname = match.nick_name && match.nick_name !== match.remark ? ` · ${match.nick_name}` : '';
      const reason = match.reason ? ` · ${match.reason}` : '';
      label.textContent = `${match.remark}${nickname} · ${identity}${reason}`;
      label.title = label.textContent;
      row.append(checkbox, label);
      rows.append(row);
    }
    list.replaceChildren(rows);
    list.hidden = !matches.length;
    if(!matches.length) count.textContent = '没有匹配的好友';
  }

  preview.addEventListener('click', async () => {
    if(!account.value){
      toast('请先选择微信账号');
      return;
    }
    busy = true;
    reset('正在匹配…');
    const requested = revision;
    try{
      const result = await post('/api/remark/preview', {account: account.value, keywords: keywords.value});
      if(requested !== revision) return;
      matches = result.matches || [];
      renderMatches();
    }catch(error){
      if(requested === revision) reset('匹配失败：' + error.message);
    }finally{
      busy = false;
      updateControls();
    }
  });

  selectAll.addEventListener('click', () => {
    const available = [...list.querySelectorAll('input:not(:disabled)')];
    const check = available.some(el => !el.checked);
    available.forEach(el => { el.checked = check; });
    updateControls();
  });

  start.addEventListener('click', async () => {
    const usernames = selectedIds();
    const message = text.value.trim();
    if(busy || !usernames.length || !message) return;
    if(!window.confirm(`确认给已勾选的 ${usernames.length} 位好友各发送一次这段文字？`)) return;
    busy = true;
    updateControls();
    try{
      const result = await post('/api/remark/start', {
        account: account.value, keywords: keywords.value, usernames, text: message,
      });
      reset(`已提交 ${result.queued} 位好友，执行结果见日志`);
      const execution = $('stopBtn').closest('details');
      if(execution){
        execution.open = true;
        execution.scrollIntoView({block:'start'});
      }
      startPolling();
    }catch(error){
      toast('启动失败：' + error.message);
    }finally{
      busy = false;
      updateControls();
    }
  });

  list.addEventListener('change', updateControls);
  text.addEventListener('input', updateControls);
  keywords.addEventListener('input', () => reset('请先预览'));
  account.addEventListener('change', () => reset('账号已切换，请重新预览'));
  updateControls();
})();
