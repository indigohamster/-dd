(function(){
  'use strict';

  const PRIMARY_VIEWS=new Set(['overview','tickets','knowledge','files','settings']);
  const VIEW_PARENTS={issues:'tickets',dashboard:'overview',monitor:'settings',network:'settings',tasks:'files','service-catalog':'settings','aitable-sync':'settings',api:'settings',models:'settings',connectors:'settings',people:'settings',response:'settings',history:'settings'};
  const VIEW_MODULES={tickets:['tickets'],issues:['tickets'],knowledge:['knowledge'],files:['spreadsheet'],dashboard:['tickets'],tasks:['spreadsheet'],'service-catalog':['tickets'],'aitable-sync':['aitable']};
  const VIEW_TITLES={overview:'首页',tickets:'工单',issues:'Issue 记录',knowledge:'知识',files:'文件与任务',settings:'设置',dashboard:'每日驾驶舱',monitor:'运行监控',network:'网络诊断',tasks:'后台任务','service-catalog':'服务目录','aitable-sync':'钉钉智能表格',api:'AI 服务',models:'多个 AI API',connectors:'连接器',people:'人员与职责',response:'回答方式',history:'配置历史'};
  const FALLBACK_MODULES=[
    {id:'tickets',name:'工单中心',description:'受理、分派、SLA 与状态通知',enabled:true},
    {id:'knowledge',name:'知识闭环',description:'从解决记录生成、审核和发布知识',enabled:true},
    {id:'spreadsheet',name:'表格助手',description:'处理 Excel、CSV 和 TSV 文件',enabled:false},
    {id:'vision',name:'图片识别',description:'识别截图、报错图片和设备照片',enabled:false},
    {id:'voice',name:'语音识别',description:'把钉钉语音转成问题并继续处理',enabled:true},
    {id:'aitable',name:'智能表格同步',description:'将工单记录同步到钉钉智能表格',enabled:false}
  ];

  const loadedViews=new Set();
  let settingsLoaded=false;
  let modules=[];
  let setupDraft={};
  let setupStep=1;
  let setupCompleted=false;
  let setupForced=false;
  let requestedParent='';

  function safeStorageGet(key){try{return localStorage.getItem(key)}catch(_){return null}}
  function safeStorageSet(key,value){try{localStorage.setItem(key,value)}catch(_){}}
  function setText(id,value){const node=$(id);if(node)node.textContent=value}
  function number(value,fallback=0){const parsed=Number(value);return Number.isFinite(parsed)?parsed:fallback}
  function currentView(){return document.querySelector('.view.active')?.id||'overview'}
  function moduleEnabled(id){const item=modules.find(module=>module.id===id);return item?item.enabled!==false:true}
  function moduleSetEnabled(value){const ids=String(value||'').split(',').map(id=>id.trim()).filter(Boolean);return !ids.length||ids.some(moduleEnabled)}
  function viewEnabled(view){return !modules.length||moduleSetEnabled((VIEW_MODULES[view]||[]).join(','))}
  function applyModuleVisibility(){
    document.querySelectorAll('[data-module-any]').forEach(node=>{node.hidden=!moduleSetEnabled(node.dataset.moduleAny)});
  }
  function mergeObjects(base,update){
    const output={...(base||{})};
    Object.entries(update||{}).forEach(([key,value])=>{const current=output[key],plainValue=value&&typeof value==='object'&&!Array.isArray(value),plainCurrent=current&&typeof current==='object'&&!Array.isArray(current);output[key]=plainValue&&plainCurrent?mergeObjects(current,value):value});
    return output
  }

  function markAdvancedFields(){
    ['ticket-filter-priority','ticket-category','ticket-assignment-group','ticket-request-type','ticket-created-from','ticket-created-to'].forEach(id=>$(id)?.closest('label')?.classList.add('advanced-setting'));
    $('ticket-batch-bar')?.classList.add('advanced-setting')
  }

  window.setAdvancedMode=function(enabled){
    document.body.classList.toggle('advanced-mode',!!enabled);
    const toggle=$('advanced-mode-toggle');if(toggle)toggle.checked=!!enabled;
    safeStorageSet('opspilot-console-mode',enabled?'advanced':'simple')
  };

  window.exportConfigPackage=function(){
    window.location.href='/api/config-package/export';
    toast('正在导出不含密钥的安全配置包')
  };

  function setConfigPackageResult(message){
    ['config-package-result','setup-config-package-result'].forEach(id=>{const node=$(id);if(node)node.textContent=message})
  }

  window.importConfigPackage=async function(file){
    if(!file)return;
    try{
      const packageData=JSON.parse(await file.text());
      const preview=await api('/api/config-package/import/preview',{method:'POST',body:JSON.stringify(packageData)});
      const sections=[...(preview.config_sections||[]),...(preview.settings_sections||[])];
      const missing=preview.missing||[];
      const summary=`将更新 ${preview.change_count??sections.length} 个配置区域${sections.length?`：${sections.join('、')}`:''}${missing.length?`。导入后仍需补充：${missing.join('、')}`:''}`;
      setConfigPackageResult(summary);
      if(!confirm(`${summary}\n\n导入前会自动保存配置历史，确定继续吗？`))return;
      const applied=await api('/api/config-package/import/apply',{method:'POST',body:JSON.stringify(packageData)});
      toast(applied.message||'配置包已导入');
      setConfigPackageResult('导入完成，正在刷新配置向导…');
      setTimeout(()=>location.reload(),800)
    }catch(error){
      setConfigPackageResult(`导入失败：${error.message}`);
      toast(`导入失败：${error.message}`,'error')
    }
  };

  window.detectAiProvider=function(baseUrl){
    const value=String(baseUrl||'').toLowerCase();
    if(value.includes('deepseek'))return'deepseek';
    if(value.includes('11434')||value.includes('ollama'))return'ollama';
    if(value.includes('openai.com'))return'openai';
    return value?'custom':'deepseek'
  };

  window.applyAiPreset=function(provider){
    const presets={deepseek:{url:'https://api.deepseek.com',path:'/v1/chat/completions',model:'deepseek-chat'},openai:{url:'https://api.openai.com',path:'/v1/chat/completions',model:''},ollama:{url:'http://127.0.0.1:11434',path:'/v1/chat/completions',model:''},custom:{url:'',path:'/v1/chat/completions',model:''}};
    const preset=presets[provider]||presets.custom;
    const key=$('llm-key'),previous=key?.dataset.provider;
    if(key&&previous&&previous!==provider)key.value='';
    if(key)key.dataset.provider=provider;
    $('llm-url').value=preset.url;$('llm-path').value=preset.path;
    if(preset.model)$('llm-model').value=preset.model;
    else $('llm-model').value='';
    $('llm-key').placeholder=provider==='ollama'?'本地服务通常无需密钥':'留空则保留现有密钥'
  };

  async function ensureSettings(){if(settingsLoaded)return;await loadSettings();settingsLoaded=true}

  async function loadHomeSummary(){
    const [dailyResult,monitorResult]=await Promise.allSettled([
      moduleEnabled('tickets')?api('/api/dashboard/daily'):Promise.resolve({}),
      api('/api/monitor/summary')
    ]);
    const daily=dailyResult.status==='fulfilled'?dailyResult.value:{};
    const monitor=monitorResult.status==='fulfilled'?monitorResult.value:{};
    const metrics=daily.metrics||{};
    const pending=number(metrics.pending),overdue=number(metrics.overdue),atRisk=number(metrics.sla_at_risk??metrics.at_risk),urgent=number(metrics.urgent);
    const notificationFailed=number(monitor.notifications_failed??metrics.notifications_failed);
    setText('home-pending',pending);setText('home-overdue',overdue);setText('home-at-risk',atRisk);setText('home-notification-failed',notificationFailed);
    const attention=moduleEnabled('tickets')?overdue+atRisk+notificationFailed:0;
    setText('home-summary-text',attention?`今天有 ${attention} 项风险需要关注，建议先处理逾期与即将超时工单。`:'当前没有明显风险，可以按计划处理待办。');
    const alerts=[];
    if(moduleEnabled('tickets')&&overdue)alerts.push({level:'error',title:`${overdue} 张工单已经超时`,detail:'立即确认负责人和最新处理进度',action:"openTicketQueue('breached')",label:'去处理'});
    if(moduleEnabled('tickets')&&atRisk)alerts.push({level:'warning',title:`${atRisk} 张工单即将超时`,detail:'在 SLA 到期前完成响应或解决',action:"openTicketQueue('at_risk')",label:'去处理'});
    if(moduleEnabled('tickets')&&notificationFailed)alerts.push({level:'error',title:`${notificationFailed} 条通知发送失败`,detail:'用户可能还没有收到工单状态更新',action:"openConsoleView('monitor','运行监控','settings')",label:'去重试'});
    if(moduleEnabled('tickets')&&urgent)alerts.push({level:'warning',title:`${urgent} 张高优先级工单待跟进`,detail:'建议在普通工单前处理',action:"openConsoleView('dashboard','每日驾驶舱','overview')",label:'查看'});
    if(moduleEnabled('knowledge')&&number(metrics.knowledge_gaps))alerts.push({level:'',title:`${number(metrics.knowledge_gaps)} 个知识缺口待补充`,detail:'完善后可减少同类人工答复',action:"openConsoleView('knowledge','知识','overview')",label:'去审核'});
    $('home-alerts').innerHTML=alerts.length?alerts.slice(0,5).map(item=>`<div class="home-alert ${item.level}"><i></i><div><b>${esc(item.title)}</b><small>${esc(item.detail)}</small></div><button class="ghost" onclick="${item.action}">${item.label}</button></div>`).join(''):'<div class="home-alert"><i></i><div><b>当前没有需要立即处理的异常</b><small>系统运行平稳，继续关注正常待办即可。</small></div></div>'
  }

  async function loadHomePage(){
    const results=await Promise.allSettled([loadOverview(),loadHomeSummary()]);
    const failed=results.find(result=>result.status==='rejected');
    if(failed)throw failed.reason
  }

  window.refreshHome=async function(){try{await loadHomePage();toast('首页已刷新')}catch(error){toast(error.message,'error')}};

  function normalizeModules(payload){
    let source=payload?.modules??payload?.items??payload?.data??payload;
    if(source&&typeof source==='object'&&!Array.isArray(source))source=Object.entries(source).map(([id,value])=>typeof value==='object'?{id,...value}:{id,enabled:!!value});
    if(!Array.isArray(source)||!source.length)return FALLBACK_MODULES.map(item=>({...item}));
    const fallbackMap=new Map(FALLBACK_MODULES.map(item=>[item.id,item]));
    return source.map((item,index)=>{
      if(typeof item==='string')item={id:item};
      const id=String(item.id||item.key||item.code||`module-${index}`);
      const defaults=fallbackMap.get(id)||{};
      return{id,name:item.name||item.label||defaults.name||id,description:item.description||item.detail||defaults.description||'',enabled:item.enabled??item.active??defaults.enabled??false,available:item.available!==false,configured:item.configured,required:item.required??defaults.required??false}
    })
  }

  function selectedModule(id,defaultValue){
    const selected=setupDraft.modules;
    if(Array.isArray(selected))return selected.includes(id);
    if(selected&&typeof selected==='object'){
      const value=selected[id];
      if(value&&typeof value==='object')return value.enabled!==false;
      if(value!==undefined)return!!value
    }
    return!!defaultValue
  }

  function renderSetupModules(){
    if(!$('setup-module-list'))return;
    const workModules=modules.filter(module=>!['dingtalk','llm'].includes(module.id));
    $('setup-module-list').innerHTML=workModules.map(module=>{const selected=selectedModule(module.id,module.enabled),wizardConfigurable=module.id==='vision',disabled=!module.available&&!selected&&!wizardConfigurable;return`<label><input type="checkbox" data-setup-module="${esc(module.id)}" ${selected?'checked':''} ${disabled?'disabled':''}><span><b>${esc(module.name)}</b><small>${esc(module.available||wizardConfigurable?module.description:`${module.description}（当前环境尚未就绪）`)}</small></span></label>`}).join('')
  }

  function renderModuleStatus(){
    if(!$('module-status-list'))return;
    $('module-status-list').innerHTML=modules.map(module=>`<label class="module-status ${module.enabled?'enabled':''}"><i></i><b>${esc(module.name)}</b><small>${module.enabled?'已启用':'未启用'}</small><input type="checkbox" ${module.enabled?'checked':''} ${module.required||(!module.available&&!module.enabled)?'disabled':''} onchange="toggleModule('${esc(module.id)}',this.checked,this)"><span class="mini-switch"></span></label>`).join('')||'<span class="module-status">尚未读取到模块</span>';
    applyModuleVisibility()
  }

  window.toggleModule=async function(moduleId,enabled,input){
    input.disabled=true;
    try{const result=await api(`/api/modules/${encodeURIComponent(moduleId)}`,{method:'PATCH',body:JSON.stringify({enabled})});toast(result.message||'模块设置已保存');await loadModules()}catch(error){input.checked=!enabled;toast(error.message,'error')}finally{input.disabled=false}
  };

  async function loadModules(){
    try{modules=normalizeModules(await api('/api/modules'))}catch(error){modules=FALLBACK_MODULES.map(item=>({...item}));console.warn('模块接口暂不可用，使用内置列表：',error)}
    renderModuleStatus();renderSetupModules();return modules
  }

  const loaders={
    overview:loadHomePage,
    tickets:async()=>{await ensureSettings();await loadTickets()},
    issues:()=>loadIssues(),
    knowledge:async()=>{await Promise.all([loadKnowledge(),loadKnowledgeAccess()])},
    files:()=>loadSpreadsheetSettings(),
    settings:async()=>{await Promise.all([loadModules(),loadKnowledgeExportDirectory()])},
    dashboard:()=>loadDashboard(),
    monitor:()=>loadMonitor(),
    network:async()=>{},
    tasks:()=>loadTasks(),
    'service-catalog':()=>ensureSettings(),
    'aitable-sync':()=>loadAITableSync(),
    api:()=>loadLlm(),
    models:()=>loadModels(),
    connectors:()=>ensureSettings(),
    people:()=>ensureSettings(),
    response:()=>ensureSettings(),
    history:()=>loadHistory()
  };

  async function loadView(view,force=false){
    if(!force&&loadedViews.has(view))return;
    const loader=loaders[view];if(!loader)return;
    try{await loader();loadedViews.add(view)}catch(error){toast(error.message||`无法加载${VIEW_TITLES[view]||'页面'}`,'error')}
  }

  window.onConsoleViewActivated=function(view){
    const parent=requestedParent||VIEW_PARENTS[view]||view;
    requestedParent='';
    document.querySelectorAll('.nav').forEach(node=>node.classList.toggle('active',node.dataset.view===parent));
    const back=$('back-to-main');
    if(back){back.hidden=PRIMARY_VIEWS.has(view);back.dataset.parent=parent;back.textContent=`返回${VIEW_TITLES[parent]||''}`}
    loadView(view)
  };

  window.openConsoleView=function(view,title,parent){
    if(!$(view)){toast('该功能页面暂不可用','error');return}
    if(!viewEnabled(view)){toast('该模块当前未启用，可在设置中开启','error');openConsoleView('settings','设置','settings');return}
    requestedParent=parent||VIEW_PARENTS[view]||view;
    activateView(view,title||VIEW_TITLES[view]);
    window.scrollTo({top:0,behavior:'smooth'})
  };

  window.openTicketQueue=function(quick){
    if(!moduleEnabled('tickets')){toast('工单模块当前未启用，可在设置中开启','error');openConsoleView('settings','设置','settings');return}
    ticketState.quick=quick||'all';currentTicketFilter=ticketState.quick;ticketState.page=1;ticketState.selected.clear();
    document.querySelectorAll('[data-ticket-quick]').forEach(node=>node.classList.toggle('active',node.dataset.ticketQuick===ticketState.quick));
    const wasLoaded=loadedViews.has('tickets');
    openConsoleView('tickets','工单','tickets');
    if(wasLoaded)loadTickets()
  };

  function populateSetupDraft(){
    const purposeMap={ops_service:'operations',knowledge_assistant:'knowledge',light_ticket:'ticket'};
    const purpose=purposeMap[setupDraft.purpose]||setupDraft.purpose||'operations';
    const purposeInput=[...document.querySelectorAll('input[name="setup-purpose"]')].find(input=>input.value===purpose);if(purposeInput)purposeInput.checked=true;
    const ding=setupDraft.dingtalk||{};$('setup-dingtalk-id').value=ding.client_id||ding.app_key||'';$('setup-dingtalk-agent').value=ding.agent_id||'';$('setup-dingtalk-secret').value='';$('setup-dingtalk-secret').placeholder=ding.has_client_secret?'已安全保存，留空保持':'粘贴密钥';
    const ai=setupDraft.ai||{};$('setup-ai-provider').value=ai.enabled===false?'none':(ai.provider||detectAiProvider(ai.base_url||''));$('setup-ai-model').value=ai.model||'';$('setup-ai-url').value=ai.base_url||'';$('setup-ai-key').value='';$('setup-ai-key').placeholder=ai.has_api_key?'已安全保存，留空保持':'粘贴密钥';
    const teamValue=setupDraft.team||{},team=Array.isArray(teamValue)?(teamValue[0]||{}):teamValue;$('setup-admin-name').value=team.admin_name||team.name||'';$('setup-admin-staff').value=team.admin_staff_id||team.staff_id||'';
    renderSetupModules();updateSetupAiFields()
  }

  function collectSetupDraft(){
    const purpose=document.querySelector('input[name="setup-purpose"]:checked')?.value||'operations';
    const dingtalk=mergeObjects(setupDraft.dingtalk,{client_id:$('setup-dingtalk-id').value.trim(),agent_id:$('setup-dingtalk-agent').value.trim()});
    const dingtalkSecret=$('setup-dingtalk-secret').value;if(dingtalkSecret)dingtalk.client_secret=dingtalkSecret;
    const provider=$('setup-ai-provider').value;
    const ai=mergeObjects(setupDraft.ai,{enabled:provider!=='none',provider,model:$('setup-ai-model').value.trim(),base_url:$('setup-ai-url').value.trim()});
    const aiKey=$('setup-ai-key').value;if(aiKey)ai.api_key=aiKey;
    const moduleIds=new Set(modules.map(module=>module.id)),moduleSelection={};
    if(setupDraft.modules&&typeof setupDraft.modules==='object'&&!Array.isArray(setupDraft.modules))Object.entries(setupDraft.modules).forEach(([id,value])=>{if(moduleIds.has(id))moduleSelection[id]=!!value});
    document.querySelectorAll('[data-setup-module]').forEach(input=>{moduleSelection[input.dataset.setupModule]=input.checked});
    if(moduleIds.has('dingtalk'))moduleSelection.dingtalk=true;if(moduleIds.has('llm'))moduleSelection.llm=provider!=='none';
    const team=[{name:$('setup-admin-name').value.trim(),staff_id:$('setup-admin-staff').value.trim(),role:'工单管理员'}];
    setupDraft=mergeObjects(setupDraft,{purpose,dingtalk,ai,modules:moduleSelection,team});return setupDraft
  }

  async function saveSetupDraft(step=setupStep){
    const draft=collectSetupDraft();
    draft.step=Math.min(5,Math.max(1,number(step,setupStep)));
    const response=await api('/api/setup/draft',{method:'PUT',body:JSON.stringify(draft)});
    const returned=response?.draft??response?.data;
    if(returned&&typeof returned==='object'){
      setupDraft=mergeObjects(setupDraft,returned);
      if(returned.dingtalk?.has_client_secret){delete setupDraft.dingtalk.client_secret;$('setup-dingtalk-secret').value='';$('setup-dingtalk-secret').placeholder='已安全保存，留空保持'}
      if(returned.ai?.has_api_key){delete setupDraft.ai.api_key;$('setup-ai-key').value='';$('setup-ai-key').placeholder='已安全保存，留空保持'}
    }
    return response
  }

  function renderSetupCheck(step,result){
    const target=$(`setup-check-${step}`);if(!target)return;
    const ok=result?.ok!==false&&result?.success!==false;
    let checks=result?.checks||result?.results||[];
    if(checks&&typeof checks==='object'&&!Array.isArray(checks))checks=Object.entries(checks).map(([name,value])=>({name,...(typeof value==='object'?value:{ok:!!value})}));
    const rows=Array.isArray(checks)?checks.map(check=>`<li>${check.ok===false?'○':'✓'} ${esc(check.name||check.label||check.component||'检查项')}：${esc(check.message||check.detail||(check.ok===false?'未通过':'正常'))}</li>`).join(''):'';
    target.className=`setup-check-result ${ok?'success':'error'}`;
    target.innerHTML=`<b>${esc(result?.message||(ok?'检查通过':'检查未通过'))}</b>${rows?`<ul>${rows}</ul>`:''}`
  }

  function setSetupError(message=''){$('setup-error').hidden=!message;$('setup-error').textContent=message}
  function setSetupBusy(busy){['setup-prev','setup-next','setup-complete'].forEach(id=>{if($(id))$(id).disabled=busy})}

  window.validateSetup=async function(step=setupStep){
    const target=$(`setup-check-${step}`);if(target){target.className='setup-check-result';target.textContent='正在检查，请稍候…'}
    setSetupError('');setSetupBusy(true);
    try{await saveSetupDraft();const result=await api('/api/setup/validate',{method:'POST',body:JSON.stringify({step,draft:setupDraft})});renderSetupCheck(step,result);return result}catch(error){renderSetupCheck(step,{ok:false,message:error.message});setSetupError(error.message);return{ok:false,message:error.message}}finally{setSetupBusy(false)}
  };

  function showSetupStep(step){
    setupStep=Math.min(5,Math.max(1,number(step,1)));
    document.querySelectorAll('[data-setup-page]').forEach(page=>page.classList.toggle('active',number(page.dataset.setupPage)===setupStep));
    document.querySelectorAll('#setup-steps li').forEach(item=>{const value=number(item.dataset.step);item.classList.toggle('active',value===setupStep);item.classList.toggle('done',value<setupStep)});
    setText('setup-progress-label',`第 ${setupStep} 步，共 5 步`);$('setup-progress-bar').style.width=`${setupStep*20}%`;
    $('setup-prev').hidden=setupStep===1;$('setup-next').hidden=setupStep===5;$('setup-complete').hidden=setupStep!==5;
    setSetupError('')
  }

  window.changeSetupStep=async function(delta){
    const next=Math.min(5,Math.max(1,setupStep+number(delta)));
    if(next===setupStep)return;
    setSetupBusy(true);setSetupError('');
    try{await saveSetupDraft(next);showSetupStep(next)}catch(error){setSetupError(error.message)}finally{setSetupBusy(false)}
  };

  window.updateSetupAiFields=function(){
    const provider=$('setup-ai-provider').value,local=['ollama','custom'].includes(provider),disabled=provider==='none';
    const key=$('setup-ai-key'),previous=key?.dataset.provider;
    if(key&&previous&&previous!==provider){key.value='';key.placeholder='请输入此服务自己的 API Key';if(setupDraft.ai&&typeof setupDraft.ai==='object'){delete setupDraft.ai.api_key;delete setupDraft.ai.has_api_key}}
    if(key)key.dataset.provider=provider;
    $('setup-ai-key-label').hidden=provider==='ollama'||disabled;$('setup-ai-model').disabled=disabled;
    $('setup-ai-url-label').classList.toggle('force-show',local);$('setup-ai-url').disabled=disabled;
    const visionModule=document.querySelector('[data-setup-module="vision"]');
    if(visionModule)visionModule.disabled=disabled;
    if(disabled){if(visionModule)visionModule.checked=false;if(setupDraft.modules&&typeof setupDraft.modules==='object')setupDraft.modules.vision=false}
    const presets={deepseek:{url:'https://api.deepseek.com',model:'deepseek-chat'},openai:{url:'https://api.openai.com',model:''},ollama:{url:'http://127.0.0.1:11434',model:''}};
    const preset=presets[provider];if(preset){if(!$('setup-ai-url').value||['https://api.deepseek.com','https://api.openai.com','http://127.0.0.1:11434'].includes($('setup-ai-url').value))$('setup-ai-url').value=preset.url;if(preset.model&&!$('setup-ai-model').value)$('setup-ai-model').value=preset.model}
  };

  function setupIsIncomplete(status){
    const completed=status?.completed??status?.setup_complete??status?.is_complete;
    if(typeof completed==='boolean')return!completed;
    return status?.required===true||status?.setup_required===true||['required','incomplete','draft'].includes(status?.status)
  }

  window.openSetupWizard=async function(force=false){
    setupForced=!!force;
    if(!modules.length)await loadModules();
    populateSetupDraft();
    $('setup-wizard').hidden=false;$('setup-close').hidden=!(setupForced||setupCompleted);document.body.classList.add('wizard-open');
    showSetupStep(force?1:setupStep)
  };

  window.closeSetupWizard=function(){
    if(!setupCompleted&&!setupForced)return;
    $('setup-wizard').hidden=true;document.body.classList.remove('wizard-open');setupForced=false
  };

  window.completeSetup=async function(){
    setSetupBusy(true);setSetupError('');
    try{
      await saveSetupDraft();
      const check=await api('/api/setup/validate',{method:'POST',body:JSON.stringify({step:'all',draft:setupDraft})});renderSetupCheck(5,check);
      if(check?.ok===false||check?.success===false)throw new Error(check.message||'上线体检未通过，请按提示修正后重试');
      const result=await api('/api/setup/complete',{method:'POST',body:JSON.stringify(setupDraft)});
      setupCompleted=true;$('setup-wizard').hidden=true;document.body.classList.remove('wizard-open');
      loadedViews.clear();settingsLoaded=false;await loadModules();openConsoleView('overview','首页','overview');toast(result.message||'配置完成，机器人已准备就绪')
    }catch(error){setSetupError(error.message);toast(error.message,'error')}finally{setSetupBusy(false)}
  };

  function applyPurposeDefaults(purpose){
    const presets={operations:['ticket','tickets','knowledge_loop','knowledge','spreadsheet','vision','voice','events','ticket_notifications'],ticket:['ticket','tickets','ticket_notifications'],knowledge:['knowledge_loop','knowledge','vision','voice'],custom:[]};
    const selected=new Set(presets[purpose]||[]);
    const aiDisabled=$('setup-ai-provider')?.value==='none';
    document.querySelectorAll('[data-setup-module]').forEach(input=>{const wanted=purpose==='operations'?input.dataset.setupModule!=='aitable':selected.has(input.dataset.setupModule);input.checked=wanted&&!input.disabled&&!(aiDisabled&&input.dataset.setupModule==='vision')});
    if(aiDisabled&&setupDraft.modules&&typeof setupDraft.modules==='object')setupDraft.modules.vision=false
  }

  async function initializeSetup(){
    try{
      const status=await api('/api/setup/status');
      setupDraft=status?.draft||status?.data?.draft||{};setupStep=number(status?.step??status?.current_step,1);setupCompleted=!setupIsIncomplete(status);
      await loadModules();populateSetupDraft();
      if(!setupCompleted){await openSetupWizard(false);return true}
    }catch(error){console.warn('无法读取首次配置状态，继续打开现有管理台：',error)}
    return false
  }

  function bindNavigation(){
    document.querySelectorAll('.nav').forEach(button=>{button.onclick=()=>openConsoleView(button.dataset.view,button.textContent,button.dataset.view)});
    $('back-to-main').onclick=()=>{const parent=$('back-to-main').dataset.parent||'settings';openConsoleView(parent,VIEW_TITLES[parent],parent)};
    document.querySelectorAll('input[name="setup-purpose"]').forEach(input=>input.addEventListener('change',()=>applyPurposeDefaults(input.value)))
  }

  async function initializeConsole(){
    markAdvancedFields();setAdvancedMode(safeStorageGet('opspilot-console-mode')==='advanced');bindNavigation();
    const blockedBySetup=await initializeSetup();if(!blockedBySetup)openConsoleView('overview','首页','overview');
    setInterval(()=>{if(!$('setup-wizard').hidden)return;const view=currentView();if(view==='overview')loadHomePage().catch(()=>{});else if(view==='tasks')loadTasks();else if(view==='monitor')loadMonitor()},30000)
    setInterval(()=>{if(!$('setup-wizard').hidden||document.hidden||currentView()!=='tickets')return;loadTickets(undefined,{silent:true})},10000)
    setInterval(()=>{if(!$('setup-wizard').hidden||document.hidden||currentView()!=='knowledge')return;loadKnowledge(currentKnowledgeFilter,{silent:true})},15000)
    document.addEventListener('visibilitychange',()=>{if(!document.hidden&&currentView()==='tickets')loadTickets(undefined,{silent:true})})
  }

  initializeConsole().catch(error=>toast(error.message,'error'))
})();
