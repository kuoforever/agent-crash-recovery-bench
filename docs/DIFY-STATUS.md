# Dify 当前状态

**结论：已在本地装好并跑起来，卡在三件事上。**

## 已完成

| 项 | 状态 |
|---|---|
| 运行位置 | WSL Ubuntu（Windows 侧无 Docker，WSL 内 Docker 28.2.2 + Compose v2.29.7） |
| 仓库 | `~/dify`（`git clone --depth 1 https://github.com/langgenius/dify.git`） |
| 版本 | Dify 1.16.1 |
| 端口 | 改为 **8088**（`docker/.env` 里 `EXPOSE_NGINX_PORT=8088`），避开 80 |
| 镜像 | 12 个全部就位 |
| 服务 | 15 个容器全部 Up，`curl http://localhost:8088/install` 在 **WSL 内**返回 200 |

### 镜像是怎么拉下来的（别踩同一个坑）

Docker Hub 直连超时（`registry-1.docker.io` timeout），但 GitHub 是通的。
**不要去改 `/etc/docker/daemon.json`**——那需要 sudo 密码。带前缀拉取再改标签即可，无需 sudo：

```bash
M=docker.m.daocloud.io
for img in $(docker compose config | grep -E "^\s+image:" | awk '{print $2}' | sort -u); do
  docker pull -q "$M/$img" && docker tag "$M/$img" "$img" && docker rmi "$M/$img"
done
```

实测可用的镜像源：`docker.m.daocloud.io`、`docker.xuanyuan.me`（`/v2/` 返回 401 即为可用）。
不可用：`dockerpull.org`、`docker.1panel.live`。

## 三个卡点

### 1. Windows 浏览器打不开

WSL 内返回 200，Windows 侧 `localhost:8088` 连接被拒、WSL 的 NAT IP 超时。
机器上没有 `.wslconfig`，走默认 NAT，端口转发没生效。

建议解法：新建 `%USERPROFILE%\.wslconfig`

```ini
[wsl2]
networkingMode=mirrored
```

然后 `wsl --shutdown` 再重启。**副作用**：会重启整个 WSL，里面其他正在跑的容器会跟着重启。

### 2. 管理员账号

首次访问 `/install` 要设管理员邮箱 + 密码，需本人在浏览器里完成。

### 3. 模型供应商 API key

「设置 → 模型供应商」要填 OpenAI API key 才能跑工作流，需本人完成。
**不要把 key 写进文档、截图或提交里。**

## 解开之后该做什么

见 `docs/HANDOFF.md` 的「接着往下做的建议」。一句话：别做 hello world，
去找它文档里写着、但用的人多半没意识到的那条语义。
