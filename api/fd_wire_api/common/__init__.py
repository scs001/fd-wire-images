"""fd_wire_api 公共小模块（跨切片共享的纯设施，零业务耦合）。

现有内容：
- :mod:`.mail`：SMTP 直发共享设施（demands 新单通知与 alerts 告警邮件
  同源调用——close-wire-implementation-gaps 1.1）。

纪律：本包只放「无业务语义」的设施件；带业务口径的渲染/收件人选择
留在各切片（demands 的运营信渲染、alerts 的告警信渲染各自持有）。
"""
