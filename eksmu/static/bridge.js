"use strict";
// Uses the existing UI helpers; no third-party requests, HTML interpolation or eval.
let verificationContext = null, verificationEpoch = 0, localCheckerKey = null;
const executionOperations = new Map();
const resultNames = {passed:tr("等式成立"),failed:tr("等式不成立"),invalid_input:tr("输入不合法"),unsupported:tr("不支持的操作"),resource_limited:tr("超出检查预算")};
function rational(raw) {
  if (!/^-?\d{1,160}(\/-?\d{1,160})?$/.test(raw)) throw new Error(tr("请输入整数或分数，例如 1/3。"));
  const [n,d="1"] = raw.split("/");
  return {op:"rational",numerator:BigInt(n).toString(),denominator:BigInt(d).toString()};
}
function taskButton(card, text, action) {
  const b = node("button",text,"secondary"); b.type="button";
  b.addEventListener("click",async () => {
    b.disabled=true;
    try { await action(); await loadVerification(); }
    catch(e) { tell(tr("验证服务：{error}。发表与讨论不受影响。",{error:e.message})); }
    finally { b.disabled=false; }
  }); card.append(b);
}
async function loadVerification() {
  if (!verificationContext) return;
  const epoch=++verificationEpoch, context=verificationContext;
  const target=$("verification-tasks"); target.replaceChildren();
  try {
    const health=await api("/api/verification/health");
    if (epoch!==verificationEpoch) return;
    localCheckerKey=health.public_key;
    $("checker-public-key").textContent=localCheckerKey || tr("尚未启用");
    $("select-local-key").disabled=!health.enabled;
    $("prepare-check").querySelector("button").disabled=!health.enabled;
    $("verification-status").textContent=health.enabled ? tr("服务已启用；每次实际计算仍需任务创建者单独允许。") : tr("服务未启用或不可用。可以继续发表、讨论和提交普通报告。");
    if (!health.enabled) return;
    const result=await api(`/api/verification/tasks?revision_id=${encodeURIComponent(context.revision.id)}`);
    if (epoch!==verificationEpoch) return;
    for (const item of result.tasks) {
      const task=item.task, card=node("article",undefined,"card");
      card.append(node("h4",tr("检查任务 {id} · {state}",{id:task.id.slice(0,8),state:{prepared:tr("仅登记"),completed:tr("已有执行回执"),cancelled:tr("已取消后续执行")}[item.state]})));
      card.append(node("p",tr("请求者：{label} · 仅针对当前查看的版本",{label:label(task.actor_id)})),node("pre",task.prepared_request.scope));
      // Show the exact input: a valid check must not be mistaken for matching the article's meaning.
      card.append(node("pre",JSON.stringify(task.prepared_request.input,null,2)));
      for (const attempt of item.attempts) {
        const r=attempt.receipt.body.result;
        card.append(node("p",tr("本机执行：{status} · {reason}",{status:resultNames[r.status] || r.status,reason:r.reason})));
        card.append(node("p",tr("运行 {id} · 可下载验签并重算；未认证原文语义。",{id:attempt.receipt.body.run_id}),"muted"));
      }
      if (task.actor_id===context.actor_id && !task.cancelled_at) {
        taskButton(card,item.attempts.length ? tr("明确允许重试（产生新回执）") : tr("明确允许本机计算"),async () => {
          // Keep the operation ID across an uncertain HTTP retry, avoid duplicate execution.
          if (!executionOperations.has(task.id)) executionOperations.set(task.id,crypto.randomUUID());
          await api("/api/verification/execute",{task_id:task.id,operation_id:executionOperations.get(task.id),consent:true});
          executionOperations.delete(task.id); tell(tr("已运行有限有理数检查；回执不代表全文成立。"));
        });
        taskButton(card,tr("取消后续检查，保留已有记录"),async () => {
          await api("/api/verification/cancel",{task_id:task.id}); tell(tr("已取消后续执行；已存在的回执和作品保留。"));
        });
      }
      taskButton(card,tr("导出任务、版本与签名回执"),async () => {
        download(await api(`/api/verification/export?task_id=${encodeURIComponent(task.id)}`),`eksmu-task-${task.id}.json`);
      });
      if (item.attempts.length) taskButton(card,tr("按所选公钥验签并重算"),async () => {
        const key=$("expected-key").value.trim();
        if (!/^[0-9a-f]{64}$/.test(key)) throw new Error(tr("请先明确选择或输入验签公钥"));
        const checked=await api("/api/verification/recheck",{task_id:task.id,expected_public_key:key});
        tell(tr("{count} 份回执的签名、版本绑定与重算匹配。这不是原创性或全文语义认证。",{count:checked.checks.length}));
      });
      target.append(card);
    }
    if (!result.tasks.length) target.append(node("p",tr("此版本尚无工具任务。无需提交任务也能自由探索。"),"muted"));
  } catch(e) {
    if (epoch===verificationEpoch) $("verification-status").textContent=tr("验证暂不可用：{error}。发表和讨论继续可用。",{error:e.message});
  }
}
document.addEventListener("eksmu:revision",event => { verificationContext=event.detail; loadVerification(); });
$("select-local-key").addEventListener("click",() => { if(localCheckerKey) $("expected-key").value=localCheckerKey; });
$("prepare-check").addEventListener("submit",async event => {
  event.preventDefault(); const b=event.currentTarget.querySelector("button");b.disabled=true;
  try {
    requireIdentity(); if (!verificationContext) throw new Error(tr("请先查看一个作品版本"));
    const d=Object.fromEntries(new FormData(event.currentTarget));
    await api("/api/verification/prepare",{revision_id:verificationContext.revision.id,scope:d.scope,
      problem:{left:{op:"add",left:rational(d.a),right:rational(d.b)},right:rational(d.c)}});
    tell(tr("任务已登记，没有执行，也没有对外发送资料。")); await loadVerification();
  } catch(e) { tell(e.message); } finally { b.disabled=false; }
});
