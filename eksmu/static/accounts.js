"use strict";
// Recovery material is temporary until confirmed; established sessions use HttpOnly cookies.
(() => {
  const pendingKey="eksmu-account-pending-v1";
  // Files and browser keys written before the 2026-10 rename used the old "oic" prefix.
  const recoveryFormats=["eksmu.recovery.v1","oic.recovery.v1"], identityKeys=["eksmu-identity-key","oic-identity-key"];
  let pending=null, busy=false, review=false, publicRegistration=false;
  try { pending=JSON.parse(sessionStorage.getItem(pendingKey)||"null"); } catch { tell(tr("待确认账户记录无法读取，请使用已保存的恢复文件登录。")); }
  function random(prefix) {
    const bytes=crypto.getRandomValues(new Uint8Array(32));
    return prefix+btoa(String.fromCharCode(...bytes)).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
  }
  function prepare(kind, extra, legacy=false) {
    if(pending) throw new Error(tr("请先处理上一次待确认操作。"));
    const value={kind,legacy,payload:{operation_id:crypto.randomUUID(),session_token:random('ois1_'),label:$("device-label").value.trim()||tr("此浏览器"),...extra}};
    sessionStorage.setItem(pendingKey,JSON.stringify(value)); // Save before sending, including on storage failure.
    pending=value; $("backup-confirmed").checked=false; update();
  }
  function update() {
    const loading=$("account-loading"); if(loading) loading.hidden=true;
    $("email-entry").hidden=!!current||!!pending||review;
    $("public-profile").hidden=!current||review;
    $("legacy-account").hidden=publicRegistration||(!!current&&!pending);
    if(pending) $("legacy-account").open=true;
    $("register").hidden=publicRegistration||!!current||!!pending||review;
    $("confirm-registration").hidden=!publicRegistration||!!current||review;
    $("rotate-recovery").hidden=publicRegistration;
    $("restore").hidden=!!current||!!pending||review;
    $("restore-file-label").hidden=!!current||!!pending||review;
    $("account-active").hidden=!current;
    $("rotate-account").hidden=!current||!!pending||review;
    $("account-pending").hidden=!pending;
    $("account-review").hidden=!review;
    const needsBackup=pending&&(pending.kind!=="login"||pending.legacy);
    $("pending-backup").hidden=!needsBackup;
    $("confirm-account").disabled=busy||!!(needsBackup&&!$("backup-confirmed").checked);
    $("cancel-account").disabled=busy;
    $("logout-account").disabled=busy||!!pending;
    $("pending-description").textContent=pending ? ({register:tr("先保存恢复凭证，再完成注册。"),rotate:tr("先保存新恢复凭证。确认后旧凭证和全部旧会话都会失效。"),login:pending.legacy?tr("检测到旧身份。先备份原密钥，再升级为可退出的设备会话。"):tr("登录结果尚待确认。可重试同一请求，不会重复创建会话。")}[pending.kind]||tr("未知待确认操作")) : "";
  }
  async function attempt() {
    if(!pending||busy) return;
    if((pending.kind!=="login"||pending.legacy)&&!$("backup-confirmed").checked) throw new Error(tr("请先下载并确认已保存恢复凭证。"));
    busy=true; update();
    try {
      const result=await api('/api/auth/'+pending.kind,pending.payload);
      if(!result.actor?.id) throw new Error(tr("服务器响应不完整，保留待确认记录。"));
      if(pending.legacy) for(const key of identityKeys) localStorage.removeItem(key);
      sessionStorage.removeItem(pendingKey); pending=null;
      $("restore").reset(); $("rotate-account").reset(); $("register").reset();
      await refresh(); tell(tr("账户已登录，当前设备可用。请私下保管恢复文件。"));
    } catch(e) {
      tell(e.status ? tr("{error}。待确认记录仍保留，可重试或取消本机待确认。",{error:e.message}) : tr("网络响应未确认。请保留恢复文件并重试，服务器可能已完成操作。"));
    } finally { busy=false; update(); }
  }
  async function render(account) {
    publicRegistration=!!account.public_registration;
    review=account.recovery_review_required; update();
    $("publication-audience").hidden=!publicRegistration;
    document.querySelector('.environment-note').textContent=publicRegistration ? tr("公开 Alpha · 人人可以探索，人人可以发表") : tr("私有开发版 · 人人可以探索，人人可以发表");
    $("email-register").querySelector('button').textContent=publicRegistration?tr("发送注册验证代码"):tr("注册并登录");
    for(const field of ['alias','password']) {$("email-register").elements[field].disabled=publicRegistration;$("registration-"+field).hidden=publicRegistration;}
    $("email-register").querySelector('button').disabled=publicRegistration&&!account.email_recovery_available;
    $("confirm-registration").querySelector('button').disabled=publicRegistration&&!account.email_recovery_available;
    $("email-auth-availability").textContent=publicRegistration ? (account.email_recovery_available ? tr("先验证邮箱，再完成注册；真实姓名或笔名均可，不要求机构邮箱。私人邮箱不会公开。") : tr("本站暂未开放新账户注册，发信服务准备好后启用。已有账户仍可登录，浏览无需账户。")) : (account.email_recovery_available ? tr("邮箱验证和邮件找回已启用；私人邮箱不会公开。") : tr("当前私有实例未配置发信服务。邮箱和密码可登录，但忘记密码暂时无法邮件找回，请先妥善保管密码。"));
    $("recovery-availability").textContent=account.email_recovery_available ? tr("验证过的邮箱可接收一次性找回代码。") : tr("当前私有实例尚未配置发信服务，暂不能发送找回邮件；请妥善保存密码。");
    $("request-reset").querySelector('button').disabled=!account.email_recovery_available;
    $("reset-password").querySelector('button').disabled=!account.email_recovery_available;
    const target=$("account-sessions"); target.replaceChildren();
    if(!current) return;
    const profile=await api('/api/auth/profile');
    const form=$("profile-form");
    form.elements.display_name.value=profile.display_name;
    form.elements.affiliation.value=profile.affiliation;
    form.dataset.version=String(profile.version);
    form.dataset.email=profile.email||"";
    $("profile-contact").textContent=profile.email ? tr("私人邮箱：{email} · {state}",{email:profile.email,state:profile.email_verified?tr("已验证"):profile.verification_available?tr("待验证"):tr("待验证；当前无法发送验证邮件")}) : tr("尚未绑定邮箱；可保留旧账户和作品，绑定后改用邮箱密码登录。");
    $("attach-email").hidden=!!profile.email;
    $("change-password-box").hidden=!profile.email;
    $("request-verify").hidden=!profile.email||profile.email_verified||!profile.verification_available;
    $("verify-email").hidden=!profile.email||profile.email_verified||!profile.verification_available;
    const result=await api('/api/auth/sessions');
    for(const session of result.sessions) {
      const box=node('div',undefined,'card');
      box.append(node('p',(()=>{const v={label:session.label,expires:new Date(session.expires_at*1000).toLocaleString(document.documentElement.lang || undefined)};return session.current?tr("{label} · 当前设备 · 有效至 {expires}",v):tr("{label} · 有效至 {expires}",v);})()));
      const button=node('button',session.current?tr("退出此设备"):tr("撤销此会话"),'secondary'); button.type='button';
      button.addEventListener('click',async()=>{
        button.disabled=true;
        try { await api('/api/auth/revoke',{session_id:session.id}); await refresh(); tell(tr("此会话已撤销。")); }
        catch(e) { tell(e.message); button.disabled=false; }
      });
      box.append(button); target.append(box);
    }
  }
  function checkDisplayName(value, email) {
    const cleaned=(value||"").trim();
    const at=cleaned.indexOf("@");
    if((at!==-1&&cleaned.slice(at+1).includes("."))||(email&&cleaned&&cleaned.toLowerCase()===String(email).trim().toLowerCase())) {
      throw new Error(tr("公开显示名称会永久公开，不能使用邮箱地址。请填写真实姓名或笔名。"));
    }
  }
  function handler(id, action) {
    $(id).addEventListener('submit',async event=>{event.preventDefault();try{await action(Object.fromEntries(new FormData($(id))));}catch(e){tell(e.message);}});
  }
  handler('register',async data=>{const alias=data.alias||tr("探索者");checkDisplayName(alias);prepare('register',{alias,recovery:random('oir1_')});});
  handler('email-register',async data=>{
    if(!publicRegistration) checkDisplayName(data.alias,data.email);
    try {const result=await api('/api/auth/email-register',{...data,label:$("device-label").value.trim()||tr("此浏览器")});$("email-register").reset();if(result.verification_required){$("confirm-registration").elements.email.value=data.email;await refresh();tell(tr("注册请求已接收。如该邮箱可以注册，验证代码将发送到私人邮箱；填入代码后才完成注册。已有账户请直接登录。"));return;}await refresh();tell(result.verification_sent?tr("账户已创建，验证代码已发送到私人邮箱。"):result.verification_available?tr("账户已创建。邮件暂未发出，可稍后请求验证代码。"):tr("账户已创建并登录。此实例尚未配置发信服务，邮件找回暂不可用。"));}
    catch(e){tell(uncertainWrite(e)?(publicRegistration?tr("注册请求结果未确认。请先查看验证邮件；已有账户可登录核对，避免连续重复请求。"):tr("注册结果未确认。请用刚填写的邮箱和密码登录核对，避免重复创建。")):e.message);}
  });
  handler('confirm-registration',async data=>{
    checkDisplayName(data.alias,data.email);
    try {await api('/api/auth/confirm-registration',{...data,label:$("device-label").value.trim()||tr("此浏览器")});$("confirm-registration").reset();await refresh();tell(tr("邮箱已验证，账户已创建并登录。可以发表了。"));}
    catch(e){tell(uncertainWrite(e)?tr("注册确认结果未确认。请用注册时的邮箱和密码登录核对；验证代码只使用一次。"):e.message);}
  });
  handler('password-login',async data=>{
    try {await api('/api/auth/password-login',{...data,label:$("device-label").value.trim()||tr("此浏览器")});$("password-login").reset();await refresh();tell(tr("已登录。"));}
    catch(e){tell(e.message);}
  });
  handler('profile-form',async data=>{
    checkDisplayName(data.display_name,$("profile-form").dataset.email);
    try {await api('/api/auth/profile',{...data,expected_version:Number($("profile-form").dataset.version)});await refresh();tell(tr("公开资料已更新。"));}
    catch(e){tell(uncertainWrite(e)?tr("保存结果未确认。请刷新页面核对公开资料。"):e.message);}
  });
  handler('attach-email-form',async data=>{
    try {const result=await api('/api/auth/attach-email',data);$("attach-email-form").reset();await refresh();tell(result.verification_sent?tr("已绑定邮箱和密码，原作品保留；验证代码已发送。"):tr("已绑定邮箱和密码，原作品保留；邮箱仍待验证。"));}
    catch(e){tell(uncertainWrite(e)?tr("绑定结果未确认。请刷新页面核对邮箱状态。"):e.message);}
  });
  handler('change-password',async data=>{await api('/api/auth/change-password',data);$("change-password").reset();await refresh();tell(tr("密码已更改，其他设备已退出。"));});
  handler('request-verify',async()=>{await api('/api/auth/request-verify',{});tell(tr("验证代码已发送。"));});
  handler('verify-email',async data=>{await api('/api/auth/verify-email',data);$("verify-email").reset();await refresh();tell(tr("邮箱已验证；旧恢复文件如有现已失效，其他设备已退出。"));});
  handler('request-reset',async data=>{await api('/api/auth/request-reset',data);tell(tr("如该邮箱可用于找回，代码已发送。"));});
  handler('reset-password',async data=>{await api('/api/auth/reset-password',data);$("reset-password").reset();await refresh();tell(tr("密码已重设，旧设备会话已退出。请用新密码登录。"));});
  handler('restore',async data=>{prepare('login',{credential:data.token.trim()});$("restore").reset();await attempt();});
  handler('rotate-account',async data=>{requireIdentity();prepare('rotate',{actor_id:current.id,credential:data.credential.trim(),new_recovery:random('oir1_')});$("rotate-account").reset();});
  $("backup").addEventListener('click',()=>{
    if(!pending) return;
    const credential=pending.payload.new_recovery||pending.payload.recovery||pending.payload.credential;
    download({format:'eksmu.recovery.v1',warning:tr("PRIVATE — 保管此文件，不要上传到社区或Git"),credential},'eksmu-recovery-private.json');
    tell(tr("恢复凭证已发起下载。确认文件已保存后再继续。"));
  });
  $("backup-confirmed").addEventListener('change',update);
  $("confirm-account").addEventListener('click',()=>attempt().catch(e=>tell(e.message)));
  $("cancel-account").addEventListener('click',()=>{
    if(!confirm(tr("取消本机待确认不会撤销服务器可能已完成的操作。请保留下载的恢复文件，以便重新登录。继续？"))) return;
    sessionStorage.removeItem(pendingKey);pending=null;update();refresh().catch(e=>tell(e.message));
  });
  $("restore-file").addEventListener('change',async()=>{
    try {
      const file=$("restore-file").files[0]; if(!file) return;
      if(file.size>4096) throw new Error(tr("恢复文件过大"));
      const value=JSON.parse(await file.text());
      const credential=recoveryFormats.includes(value.format)?value.credential:value.token;
      if(typeof credential!=='string'||credential.length>128) throw new Error(tr("恢复文件格式不正确"));
      $("restore").elements.token.value=credential;
      tell(tr("恢复凭证已读入，点击登录即可。"));
    } catch(e) { tell(e.message); }
    finally { $("restore-file").value=''; }
  });
  $("logout-account").addEventListener('click',async()=>{
    try {
      await api('/api/auth/logout',{});
      if (typeof setAccountTab === "function") setAccountTab(true);
      await refresh();
      tell(tr("当前设备已退出。恢复文件可用于再次登录；本机草稿仍按原身份保存。"));
    }
    catch(e) { tell(tr("退出尚未确认，请重试。{error}",{error:e.message})); }
  });
  $("logout-others").addEventListener('click',async()=>{
    try { await api('/api/auth/revoke-others',{});await refresh();tell(tr("其他会话已撤销，当前设备仍可使用。")); }
    catch(e) { tell(e.message); }
  });
  let setAccountTab = null;
  function initTabs() {
    const tabLogin = $("tab-login"), tabRegister = $("tab-register");
    const paneLogin = $("pane-login"), paneRegister = $("pane-register");
    if (!tabLogin || !tabRegister || !paneLogin || !paneRegister) return;
    setAccountTab = function(isLogin) {
      tabLogin.className = isLogin ? "account-tab on" : "account-tab";
      if (tabLogin.setAttribute) tabLogin.setAttribute("aria-selected", isLogin ? "true" : "false");
      paneLogin.hidden = !isLogin;
      tabRegister.className = !isLogin ? "account-tab on" : "account-tab";
      if (tabRegister.setAttribute) tabRegister.setAttribute("aria-selected", !isLogin ? "true" : "false");
      paneRegister.hidden = isLogin;
    };
    if (tabLogin.addEventListener) tabLogin.addEventListener("click", () => setAccountTab(true));
    if (tabRegister.addEventListener) tabRegister.addEventListener("click", () => setAccountTab(false));
    if (typeof location !== "undefined" && location.hash === "#register") setAccountTab(false);
  }
  initTabs();
  window.EksmuAccounts={render,get publicMode(){return publicRegistration;},async start(){
    await refresh();
    const legacy=identityKeys.map(key=>localStorage.getItem(key)).find(Boolean);
    if(!publicRegistration&&!current&&!pending&&legacy&&!review) prepare('login',{credential:legacy},true);
    update();
  }};
})();
