# 红薯雷达 · Rednote Watchdesk

在电脑上运行的小红书账号监测工具。每个目标可以填写多个候选小红书号，任一候选命中后就停止搜索同组其他候选。命中后可发送飞书通知，也可用选定的账号自动关注。

![监控目标页面（演示数据）](docs/screenshot.png)

## 快速开始

需要 Python 3.10+、Google Chrome 和桌面环境。主要在 macOS 上验证，Linux 可手动启动。

```sh
git clone https://github.com/CBOSSX/rednote-watchdesk.git
cd rednote-watchdesk
./setup.sh
./run.sh
```

打开 http://127.0.0.1:8767

1. 在「登录账号」添加账号并扫码登录。在「搜索账号顺序」加入账号，排好优先级后保存。
2. 在「监控目标」添加目标和候选小红书号。如需自动关注，在目标卡片里选择执行账号，然后保存配置。
3. 在「轮询与通知」调整查询间隔。需要飞书通知时，填写自定义机器人的 Webhook，保存后发送测试卡片。

<details>
<summary>查看账号管理与关注选择（演示数据）</summary>

![账号管理页面](docs/accounts.png)

<img src="docs/follower-selection.png" alt="自动关注账号选择弹窗" width="480">

</details>

监测期间请保持电脑开机、联网，并保持系统用户登录。发现时间取决于轮询间隔、小红书的搜索收录和平台限制。

## macOS 后台运行

想让程序在登录 macOS 后自动运行，可先退出手动启动的 `./run.sh`，再安装后台服务。之后直接打开本机页面即可。

```sh
.venv/bin/python -m scripts.install_macos
```

卸载服务会保留本地数据。

```sh
.venv/bin/python -m scripts.install_macos --uninstall
```

## 本地数据

配置、运行记录和账号登录信息保存在仓库外。请勿分享或提交这些目录。

- macOS · `~/Library/Application Support/RednoteWatchdesk/`
- Linux · `$XDG_DATA_HOME/rednote-watchdesk/`，默认是 `~/.local/share/rednote-watchdesk/`

[MIT 协议](LICENSE)
