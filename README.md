# Wonder Isle · Development Preview

独立生活岛开发试玩版。此仓库只负责构建和部署预览，不包含正式源码历史。

- 试玩地址：https://zhuwd-dev.github.io/wonder-isle-preview/
- 源码仓库：https://github.com/zhuwd-dev/wonder-isle
- 源码分支：`feature/life-island-polish`
- 当前默认源码 SHA：`1ac09de449bf7a8a0953f4ca7ef037c837791c2e`

## 更新试玩版本

打开本仓库的 GitHub Actions，运行 `Deploy Development Preview`，将 `source_sha` 填为需要试玩的完整 40 位源码提交 SHA。工作流会从公开源码仓库检出该提交，使用 Godot 4.7.2 导出 Web 版，执行回归检查并部署到本仓库 Pages。部署页面显示的 `BUILD_COMMIT.txt` 和游戏版本标识均对应源码 SHA。

试玩版使用独立的 preview 存档文件、localStorage 镜像、IndexedDB 保存账本及 PWA 缓存。正式动物动画素材仍在制作中（VISUAL BLOCKED）。

正式版网站与存档不由此仓库构建或修改。