// User-provided PPO example with TeX subscripts, not Markdown-escaped underscores.
export const PPO_MATH = String.raw`定义重要性采样比率（Importance Sampling Ratio）：\
$$ r_t(\theta) = \frac{\pi_\theta(a_t | s_t)}{\pi_{\theta_{\text{old}}}(a_t | s_t)} $$

其中 $\pi_\theta$ 是当前策略网络，$\pi_{\theta_{\text{old}}}$ 是生成数据的旧策略网络。

PPO 的核心目标函数定义为：\
$$ L^{CLIP}(\theta) = \hat{\mathbb{E}}_t \left[ \min \left( r_t(\theta)\hat{A}_t, \text{clip}\left(r_t(\theta), 1-\epsilon, 1+\epsilon\right)\hat{A}_t \right) \right] $$

- **$\hat{A}_t$**：优势函数（Advantage Function）。`
