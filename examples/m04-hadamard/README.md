# M04：有限 Sylvester 矩阵构造（演示）

本材料从 `H₁=[1]` 出发，以 `H₂ₙ=[[Hₙ,Hₙ],[Hₙ,−Hₙ]]` 构造 1、2、4、8、16 阶矩阵。[数据](matrices.json)由[生成器](generate.py)生成，[检查器](check.py)独立逐项检查只含 `±1`、块递推及整数恒等式 `HHᵀ=nI`。不使用浮点运算。

在仓库根目录运行：

```bash
python examples/m04-hadamard/check.py
python examples/m04-hadamard/generate.py
python examples/m04-hadamard/check.py
```

块乘法给出 `H₂ₙH₂ₙᵀ=diag(2HₙHₙᵀ,2HₙHₙᵀ)`，所以已知 `HₙHₙᵀ=nI` 时，递推后的矩阵满足 `H₂ₙH₂ₙᵀ=2nI`。基例 `H₁H₁ᵀ=I`。这解释了所有二的幂阶构造；保存的数据及程序只实际运行到 16 阶。它没有解决其他阶的 Hadamard 构造问题，也不是奖项投稿或独立人员复核。

参与者可独立重写乘法检查器、扩展阶数或形式化递推引理。问题卡见数学参考卡 M04。发表和讨论无需运行本程序。
