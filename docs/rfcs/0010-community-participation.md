# RFC 0010：从发表工具到持续参与的社区

日期：2026-09-20。状态：本地实现并测试。基线：主分支193b826。

## 产品目标

用户要求继续积极建设社区，减少过度保守和防御性的叙述；先把产品做好，然后宣传并聚集参与者。本轮选择补全发现、协作、关注和回应闭环，不继续制造平行验证协议。

新参与者应能找到一个感兴趣的问题，发表自己的尝试，让别人知道需要什么帮助，并在有新回应时回来继续。

## 设计

`oic.community.Community`读取既有作品记录快照，在独立SQLite库维护主题介绍、变更记录、个人关注/收藏和已读状态。新增服务不写原作品数据库。介绍使用expected_version检测并发编辑。

主题由作者自由选择，每项最多8个，进行Unicode规范化；分类不决定发表资格。“寻找协作”由作者填写希望获得的帮助触发，而非专家审核。“等待他人回应”区分作者自评与他人参与。

关注与收藏分开，均不公开为人气分。通知由确定的作品事件产生：自己作品收到回应、自己的评论/报告被回复、关注后的新事件。自己的活动不提醒自己，同一记录只出现一条通知。逐条标记已读防止刷新竞争导致新提醒丢失。

搜索匹配最新版标题/正文及主题、协作介绍；提供最近发表和最近活动两种顺序。个人筛选需要身份，但匿名阅读和基本发表不依赖此服务。

前端继续使用同一个应用和同一身份，新增模块通过现有状态与版本事件连接。旧工具工作台保留为可展开区域。个人清单可以导出/恢复，当前只支持已有对应作品和同一身份的本地迁移。

## 接口

GET `/api/community/feed`：q、topic、view、order、offset、limit。view为all/unanswered/collaborate/following/bookmarks/mine。

GET `/api/community/topics`、`details?exploration_id=…`：主题和作品介绍。

POST `/api/community/details`：exploration_id、topics、help、expected_version。

POST `/api/community/mark`：exploration_id、kind（follow/bookmark）、enabled（布尔值）。

GET `/api/community/notifications`；POST `/api/community/read`：record_ids。

GET `/api/community/export`；POST `/api/community/import`：bundle。个人接口沿用既有身份令牌，不接收客户端自称的actor_id或权限字段。

## 完成与下一步

已实现主题、协作介绍、检索、个人清单、站内提醒、版本直达链接和两人HTTP演示。新增47项Python测试和16项隔离界面测试。

下一步把长文、公式、图片/代码材料的表达体验和真正的工具验证报告接入同一探索页面，再完成面向首批试用者的部署与账户体验。浏览器延期不阻塞这些开发；完整浏览器实测仍是对真实试用体验的后续检查。
