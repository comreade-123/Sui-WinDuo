# Git 与 GitHub 操作指南（Windows PowerShell）

本文档从零开始，带你把这个仓库从本地目录发布到 `https://github.com/comreade-123`，
并覆盖日常提交流程、打标签发布与常见错误处理。所有命令均为 **Windows PowerShell** 语法，在仓库根目录执行。

> 仓库根目录示例：`D:\Deepseek-API-project\DSH\WinDuo-EthanMaven`
> 下文出现的仓库名 `Sui-WinDuo` 只是示例，你可以换成自己喜欢的名字。

## 目录

- [0. 前置条件](#0-前置条件)
- [1. 一次性全局配置](#1-一次性全局配置)
- [2. 初始化本地仓库](#2-初始化本地仓库)
- [3. 让 .gitignore 正确生效](#3-让-gitignore-正确生效)
- [4. 第一次提交](#4-第一次提交)
- [5. 在 GitHub 网页上创建仓库](#5-在-github-网页上创建仓库)
- [6. 关联远程仓库并首次推送](#6-关联远程仓库并首次推送)
- [7. 认证方式：HTTPS + PAT 或 SSH](#7-认证方式https--pat-或-ssh)
- [8. 日常提交流程](#8-日常提交流程)
- [9. 分支与 Pull Request](#9-分支与-pull-request)
- [10. 打标签发布版本](#10-打标签发布版本)
- [11. 常见错误与处理](#11-常见错误与处理)
- [12. 可选：GPG 签名](#12-可选gpg-签名)
- [13. 本仓库的结构注意事项](#13-本仓库的结构注意事项)
- [14. 命令速查表](#14-命令速查表)

---

## 0. 前置条件

1. 安装 **Git for Windows**：<https://git-scm.com/download/win>（安装时保持默认选项即可，包含 Git Credential Manager）。
2. 打开 **PowerShell**，确认安装成功：

   ```powershell
   git --version
   ```

   预期输出形如 `git version 2.4x.x.windows.1`。

3. 进入仓库根目录：

   ```powershell
   cd D:\Deepseek-API-project\DSH\WinDuo-EthanMaven
   ```

4. 如果 PowerShell 里中文显示为乱码，先执行一次（仅当前窗口有效）：

   ```powershell
   chcp 65001
   ```

---

## 1. 一次性全局配置

设置提交者身份（会写进每一个 commit，请填你自己的信息）：

```powershell
git config --global user.name "EthanMaven"
git config --global user.email "你的邮箱@example.com"
```

**作用**：Git 必须知道是谁提交的；邮箱建议与 GitHub 账号一致，这样提交才能正确归属到你的头像。

推荐同时配置以下几项（Windows 上尤其有用）：

```powershell
# 提交时统一用 LF，检出到工作区时用 CRLF，避免「LF will be replaced by CRLF」警告
git config --global core.autocrlf true

# 让 git status 正常显示中文文件名，而不是 \344\275\240 这种转义
git config --global core.quotepath false

# 使用 Windows 自带的证书后端，减少企业网络下的 SSL 报错
git config --global http.sslBackend schannel

# 默认分支名统一为 main
git config --global init.defaultBranch main

# 拉取时默认使用 rebase，历史更线性
git config --global pull.rebase true
```

查看当前配置：

```powershell
git config --global --list
```

---

## 2. 初始化本地仓库

在仓库根目录执行：

```powershell
git init -b main
```

**作用**：在当前目录创建 `.git` 子目录，把这里变成一个 Git 仓库，并把初始分支命名为 `main`。

> `-b main` 需要 Git 2.28 及以上版本。若提示不支持，改用：

```powershell
git init
git branch -M main
```

**作用**：先初始化（默认分支可能是 `master`），再把当前分支重命名为 `main`。

查看当前状态：

```powershell
git status
```

**作用**：列出未跟踪（untracked）与已修改（modified）的文件。此时应当能看到 `README.md`、`LICENSE`、
`firmware/`、`pc/` 等条目，而 `build/`、`__pycache__/` 之类应当**不出现**（被 `.gitignore` 忽略）。

确认没有被误跟踪的大文件与产物：

```powershell
git status --short
```

---

## 3. 让 .gitignore 正确生效

`.gitignore` **只对「尚未被跟踪」的文件生效**。如果你在写 `.gitignore` 之前已经 `git add` 过编译产物，
它们会继续被跟踪。处理办法是清空索引后重新添加：

```powershell
# 清空索引（不会删除工作区里的任何文件）
git rm -r --cached .

# 重新按 .gitignore 规则添加
git add .

# 查看结果
git status
```

**作用**：`--cached` 表示只操作索引，不动磁盘文件；重新 `git add` 时被忽略的文件就不会再进入待提交列表。

强制检查某个路径是否被忽略：

```powershell
git check-ignore -v build
```

**作用**：打印匹配到的 `.gitignore` 规则与行号；如果没有输出，说明该文件**没有**被忽略。

> 本仓库的 `.gitignore` 已忽略：`build/`、`*.bin`、`*.elf`、`*.map`、`*.hex`、`.vscode/`、`arduino_secrets.h`、
> `__pycache__/`、`*.pyc`、`.venv/`、`.idea/`、`*.log`、`sample_stream.jsonl` 等。
> 注意：**`*.ino` 源码不会被忽略**，固件草图必须提交。

---

## 4. 第一次提交

```powershell
# 添加全部改动（受 .gitignore 控制的文件不会被加入）
git add .

# 提交并写清楚信息
git commit -m "chore: 初始化 Sui-WinDuo 仓库"

# 查看提交历史
git log --oneline -n 5
```

**作用**：`git add` 把改动放进暂存区（staging area），`git commit` 把暂存区固化成一次历史记录。

只想提交部分文件时，显式列出路径：

```powershell
git add README.md docs\git_guide.md
git commit -m "docs: 补充 Git 操作指南"
```

---

## 5. 在 GitHub 网页上创建仓库

1. 登录 <https://github.com/comreade-123>。
2. 右上角 **+** → **New repository**。
3. **Repository name** 填 `Sui-WinDuo`。
4. **Description** 可填：`ESP32 + MPU6050 笔记本屏幕开合角传感器（WindowsDuo 二创版）`。
5. 选择 **Public**（开源）或 **Private**。
6. **不要勾选** `Add a README file`、`Add .gitignore`、`Choose a license`——
   本地已经有这些文件，勾选会在推送时造成「远程有本地没有的提交」而需要额外合并。
7. 点击 **Create repository**，复制页面上给出的 HTTPS 地址，形如：

   ```text
   https://github.com/comreade-123/Sui-WinDuo.git
   ```

---

## 6. 关联远程仓库并首次推送

```powershell
# 关联远程仓库，命名为 origin
git remote add origin https://github.com/comreade-123/Sui-WinDuo.git

# 确认关联结果
git remote -v

# 首次推送，并把本地 main 与远程 main 建立跟踪关系
git push -u origin main
```

**作用**：
- `git remote add` 只是记一个「远程地址的别名」，不会立刻联网；
- `git push -u` 中 `-u`（即 `--set-upstream`）让本地 `main` 跟踪 `origin/main`，之后直接 `git push` / `git pull` 即可，无需再写远程名与分支名。

地址写错了可以改：

```powershell
git remote set-url origin https://github.com/comreade-123/Sui-WinDuo.git
git remote remove origin   # 需要删除时使用
```

---

## 7. 认证方式：HTTPS + PAT 或 SSH

GitHub 早已**不再支持用账号密码推送**。两种可行方式：

### 方式 A：HTTPS + Personal Access Token（推荐新手）

1. 打开 <https://github.com/settings/tokens> → **Generate new token (classic)**。
2. 勾选 **repo** 权限，设置有效期，生成后**立即复制**（页面刷新后不再显示）。
3. 推送时：
   - 用户名填 `comreade-123`；
   - 密码处粘贴 **Token**（不是账号密码）。
4. 首次输入后由 Windows **凭据管理器**记住，后续不再询问。

也可以用 Git Credential Manager 走浏览器登录（安装 Git for Windows 时默认自带）：

```powershell
git config --global credential.helper manager
```

### 方式 B：SSH 密钥

```powershell
# 1. 生成密钥（一路回车即可，或用 -f 指定路径）
ssh-keygen -t ed25519 -C "你的邮箱@example.com"

# 2. 复制公钥内容
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub

# 3. 把公钥粘贴到 https://github.com/settings/keys → New SSH key

# 4. 测试连接（首次会询问是否信任主机，输入 yes）
ssh -T git@github.com

# 5. 把远程地址切换为 SSH
git remote set-url origin git@github.com:comreade-123/Sui-WinDuo.git
```

**作用**：SSH 用密钥对认证，不需要每次输入凭据。

---

## 8. 日常提交流程

每次修改后按这个顺序走：

```powershell
# 1. 看改了什么
git status
git diff

# 2. 加入暂存区
git add .

# 3. 提交（信息要能说明「做了什么」）
git commit -m "fix: 修正按键长按判定的边界"

# 4. 先拉取远程改动（保持历史线性），再推送
git pull --rebase
git push
```

**作用**：`git pull --rebase` 会先取回远程提交，再把你的本地提交「叠」到最新提交之上，避免产生多余的合并节点。

撤销与回退（谨慎使用）：

```powershell
# 撤销工作区对某个文件的修改（未 add 时）
git restore README.md

# 把已 add 的文件移出暂存区（保留改动）
git restore --staged README.md

# 追加到上一次提交（还没推送时）
git add . ; git commit --amend --no-edit

# 查看某文件的改动历史
git log --oneline -- README.md
```

---

## 9. 分支与 Pull Request

```powershell
# 新建并切换到功能分支
git switch -c feat/oled-refresh

# 在分支上提交后推送
git push -u origin feat/oled-refresh
```

**作用**：推送后 GitHub 页面会出现 **Compare & pull request** 按钮，点进去填写说明即可发起 PR；
合并后回到本地：

```powershell
git switch main
git pull --rebase
git branch -d feat/oled-refresh        # 删除已合并的本地分支
git push origin --delete feat/oled-refresh   # 删除远程分支（可选）
```

命名建议：`feat/`、`fix/`、`docs/`、`test/`、`chore/` 前缀，一眼能看出改动类型。

---

## 10. 打标签发布版本

版本号建议遵循语义化版本 `主版本.次版本.修订号`：

```powershell
# 创建带说明的附注标签
git tag -a v1.0.0 -m "首个公开版本：ESP32 固件 + PC 端玻璃效果"

# 查看标签
git tag -l

# 查看某个标签的详情
git show v1.0.0

# 推送单个标签
git push origin v1.0.0

# 或一次性推送所有本地标签
git push --tags
```

**作用**：标签是给某个提交起的「版本名」。推送标签后，在 GitHub 的 **Releases** 页面
点 **Draft a new release** 选择该标签，填写发布说明即可发布正式版本。

删除标签：

```powershell
git tag -d v1.0.0                    # 删除本地标签
git push origin :refs/tags/v1.0.0    # 删除远程标签
```

---

## 11. 常见错误与处理

### 11.1 `remote: Support for password authentication was removed` / 403 鉴权失败

- 原因：使用了账号密码，或 Token 权限不足 / 已过期。
- 处理：改用 **PAT**（勾选 `repo` 权限）或 **SSH**（见 [第 7 节](#7-认证方式https--pat-或-ssh)）。
- 若之前存了错误凭据，清掉再试：

  ```powershell
  # 打开 Windows 凭据管理器，删除 git:https://github.com 条目
  control /name Microsoft.CredentialManager
  ```

### 11.2 `! [rejected] main -> main (fetch first)` / 非快进（non-fast-forward）

- 原因：远程有本地没有的提交（常见于在网页上直接改过文件）。
- 处理：

  ```powershell
  git pull --rebase origin main
  git push
  ```

- 确实想用本地历史覆盖远程时（**会丢失远程提交，谨慎**）：

  ```powershell
  git push --force-with-lease
  ```

### 11.3 `refusing to merge unrelated histories`

- 原因：本地与远程是两个互不相关的历史（例如建仓时勾选了初始化 README）。
- 处理：

  ```powershell
  git pull origin main --allow-unrelated-histories
  ```

### 11.4 文件过大被拒绝（`GH001` / `this exceeds GitHub's file size limit of 100.00 MB`）

- GitHub 单文件超过 **100MB** 会被拒绝，超过 **50MB** 会收到警告。
- 处理：把编译产物、模型、录屏等加入 `.gitignore`，不要提交；确需版本管理大文件时使用 Git LFS：

  ```powershell
  git lfs install
  git lfs track "*.bin"
  git add .
  ```

  （`git lfs track` 会在仓库根目录写入 LFS 规则文件，按其提示一并提交即可。）

- 如果大文件**已经在历史提交里**，需要重写历史（例如 `git filter-repo`）或改用新的仓库重新推送。

### 11.5 换行符警告 `LF will be replaced by CRLF`

- 原因：Windows 默认检出为 CRLF，而仓库内以 LF 存储。
- 处理：保持 `git config --global core.autocrlf true` 即可；这只是提示，不影响内容。
- 需要强制统一时：`git config --global core.autocrlf input`（提交时转为 LF，检出不做转换）。

### 11.6 中文文件名显示为转义字符

```powershell
git config --global core.quotepath false
```

### 11.7 无法连接 github.com / 连接超时 / `Could not resolve host`

- 检查网络与代理设置；若使用了代理：

  ```powershell
  git config --global http.proxy http://127.0.0.1:端口
  git config --global https.proxy http://127.0.0.1:端口
  # 取消代理
  git config --global --unset http.proxy
  git config --global --unset https.proxy
  ```

- 同时检查系统 `hosts` 文件（`C:\Windows\System32\drivers\etc\hosts`）里是否有把 `github.com` 指向异常地址的条目。

### 11.8 PowerShell 里提交信息含中文出现乱码

- 先执行 `chcp 65001` 切换到 UTF-8 代码页再提交；
- 也可以用 `git commit -F <文件>`，把提交信息写在 UTF-8 编码的文本文件里再读取。

### 11.9 误提交了不该提交的文件

```powershell
# 从索引中移除（保留本地文件），随后提交
git rm --cached arduino_secrets.h
git commit -m "chore: 移除误提交的本地配置"
```

- 若敏感信息（Token、Wi-Fi 密码）已推送，**必须立即在服务端作废该凭据**，仅删除文件是不够的。

---

## 12. 可选：GPG 签名

需要让提交显示 **Verified** 徽章时使用：

```powershell
# 1. 生成密钥（选择 RSA 4096 或 Ed25519，设置好邮箱）
gpg --full-generate-key

# 2. 列出密钥，复制私钥 ID（长格式最后一列，如 3AA5C34371567BD2）
gpg --list-secret-keys --keyid-format=long

# 3. 告诉 Git 用哪个密钥
git config --global user.signingkey 3AA5C34371567BD2

# 4. 默认对所有提交签名
git config --global commit.gpgsign true

# 5. 把公钥内容粘贴到 https://github.com/settings/keys → New GPG key
gpg --armor --export 3AA5C34371567BD2
```

签名标签：

```powershell
git tag -s v1.0.0 -m "签名发布 v1.0.0"
```

---

## 13. 本仓库的结构注意事项

本仓库是**纯 Arduino IDE 工程**，提交时请注意：

- 固件的**唯一草图文件**是：

  ```text
  firmware\WindowsDuo_EthanMaven\WindowsDuo_EthanMaven.ino
  ```

  Arduino IDE 要求 `.ino` 文件名与所在文件夹同名，**不要重命名该文件，也不要改变它的目录层级**。
- 需要提交的源码：`firmware/`、`pc/`、`tools/`、`verification/`、`docs/`、`README.md`、`LICENSE`、
  `CONTRIBUTING.md`、`.gitignore`。
- **不要提交**：`build/`、`*.bin`、`*.elf`、`*.map`、`*.hex`、`__pycache__/`、`.venv/`、
  `arduino_secrets.h`、`sample_stream.jsonl` 等（已在 [.gitignore](../.gitignore) 中忽略）。
- 换行符无需特殊处理，保持 `core.autocrlf true` 即可。

---

## 14. 命令速查表

| 目的 | 命令 |
| --- | --- |
| 查看状态 | `git status` |
| 查看改动内容 | `git diff` |
| 加入暂存区 | `git add .` |
| 提交 | `git commit -m "说明"` |
| 查看历史 | `git log --oneline -n 10` |
| 关联远程 | `git remote add origin <URL>` |
| 首次推送 | `git push -u origin main` |
| 日常推送 | `git pull --rebase` → `git push` |
| 新建分支 | `git switch -c feat/xxx` |
| 打标签 | `git tag -a v1.0.0 -m "说明"` |
| 推送标签 | `git push --tags` |
| 检查是否被忽略 | `git check-ignore -v 路径` |
| 清空索引重来 | `git rm -r --cached .` → `git add .` |

---

相关文档：[README.md](../README.md)（项目总览）、[CONTRIBUTING.md](../CONTRIBUTING.md)（贡献流程）、
[docs/hardware_checklist.md](hardware_checklist.md)（实物验证清单）。
