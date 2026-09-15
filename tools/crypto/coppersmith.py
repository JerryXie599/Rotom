#!/usr/bin/env python3
"""单变量 Coppersmith(Howgrave-Graham 格攻击)——纯 Python + sympy 的 LLL,离线可用。

用途(线下赛/靶场常见):
  1. 部分密钥泄露:已知 p 的高位和/或低位,求中间未知位(模未知因子,beta=0.5);
  2. 小根:已知 m 的线性结构(如 m = prefix*2^k + x),求 x(模 N,beta=1);
  3. stereotyped message / 广播攻击的参数不当时。

格约简后端:pwn64 里已装 **fpylll**(apt 的 python3-fpylll,快);没有 fpylll 时自动回退 **sympy.Matrix.lll()**(慢)。
参数调法:m 越大界越宽但格越慢;t = floor(d*m*(1/beta - 1))。

用法(作为库):
    from coppersmith import coppersmith_univariate
    xs = coppersmith_univariate(f_coeffs_low2high, N, beta=0.5, X=2**459, mm=8)
    # f_coeffs_low2high: f(x) 的系数(常数项在前),要求首一(monic)

命令行:
    python3 coppersmith.py --n N --coeffs "c0,c1,c2" --beta 0.5 --xbits 459 [--m 8] [--t 4]
"""

from __future__ import annotations

import argparse
import sys
from math import gcd

import sympy
from sympy import Poly, Matrix, ZZ, symbols

try:                                    # 快后端(推荐)
    from fpylll import IntegerMatrix, LLL as _FpyLLL
except Exception:                       # 回退:sympy 自带 LLL(慢)
    IntegerMatrix = None
    _FpyLLL = None


def _poly_from_coeffs(coeffs: list[int]):
    x = symbols("x")
    expr = sum(int(c) * x**i for i, c in enumerate(coeffs))
    return Poly(expr, x, domain=ZZ)


def coppersmith_univariate(coeffs: list[int], N: int, beta: float, X: int,
                           mm: int | None = None, tt: int | None = None,
                           verbose: bool = False) -> list[int]:
    """求 f(x) ≡ 0 mod b 的小根 x0,|x0| <= X,其中 b >= N^beta 且 b | N。

    返回所有找到的整数根。找不到返回 []。
    """
    f = _poly_from_coeffs(coeffs)
    d = f.degree()
    if d < 1:
        return []
    # 首一化:乘 LC 在 mod N 下的逆(不改变 mod b 的根,f 非首一时必须做)
    lc = int(f.LC())
    if lc != 1:
        try:
            inv = pow(lc % N, -1, N)
        except ValueError:
            return []                      # LC 与 N 不互素,无法首一化
        coeffs = [((int(c) * inv) % N) for c in coeffs]
        f = _poly_from_coeffs(coeffs)
    if mm is None:
        mm = max(1, int(round(beta * d / 0.03)) // max(1, d)) or 1
        mm = max(2, min(mm, 12))
    if tt is None:
        tt = max(0, int(d * mm * (1 / beta - 1)))

    # 构造格:g_{i,j}(x) = x^j * N^(m-i) * f(x)^i  (i=0..m, j=0..d-1)
    #         g_{m,j}(x) = x^j * f(x)^m            (j=0..t-1)
    polys: list[Poly] = []
    x = symbols("x")
    # 注意:第一族只到 i=m-1,否则 x^0·f^m 会与第二族首行重复,导致格秩亏(sympy LLL 直接报错)
    for i in range(mm):
        for j in range(d):
            polys.append(Poly(x**j * N ** (mm - i) * f.as_expr() ** i, x, domain=ZZ))
    for j in range(tt):
        polys.append(Poly(x**j * f.as_expr() ** mm, x, domain=ZZ))

    dim = len(polys)
    # 系数矩阵(按 x^k 的系数,先按 X 缩放)
    mat = Matrix.zeros(dim, dim)
    for r, p in enumerate(polys):
        for k in range(dim):
            c = p.coeff_monomial(x**k)
            if c:
                mat[r, k] = int(c) * X**k
    if verbose:
        print(f"[coppersmith] deg={d} m={mm} t={tt} dim={dim} X≈2^{X.bit_length()-1}", file=sys.stderr)

    if IntegerMatrix is not None:        # fpylll:快很多(实测 119s → 秒级)
        A = IntegerMatrix(dim, dim)
        for r in range(dim):
            for c in range(dim):
                v = int(mat[r, c])
                if v:
                    A[r, c] = v
        _FpyLLL.reduction(A)
        rows = [[int(A[r, c]) for c in range(dim)] for r in range(dim)]
    else:
        red = mat.lll()
        rows = [[int(red[r, c]) for c in range(dim)] for r in range(dim)]
    # 取最短向量还原成多项式,并验证
    roots: set[int] = set()
    for r in range(len(rows)):
        row = [rows[r][k] // X**k if X**k else 0 for k in range(dim)]
        if all(v == 0 for v in row):
            continue
        g = Poly(sum(row[k] * x**k for k in range(dim)), x, domain=ZZ)
        if g.degree() < 1:
            continue
        try:
            for root in sympy.real_roots(g.as_expr(), x):
                if root.is_integer:
                    cand = int(root)
                    if abs(cand) <= X:
                        # 验证:f(cand) 与 N 有非平凡公因子(或整除),即为真解
                        fv = int(f.eval(cand))
                        for div in (N, gcd(abs(fv), N)):
                            if div > 1 and fv % div == 0:
                                roots.add(cand)
                                break
        except Exception:
            continue
    return sorted(roots)


def main() -> int:
    ap = argparse.ArgumentParser(description="单变量 Coppersmith(离线,基于 sympy LLL)")
    ap.add_argument("--n", required=True, help="模数 N")
    ap.add_argument("--coeffs", required=True, help="f(x) 系数,低次在前,逗号分隔(需首一)")
    ap.add_argument("--beta", type=float, default=1.0, help="因子下界:N^beta | N(部分密钥泄露常为 0.5)")
    ap.add_argument("--xbits", type=int, required=True, help="根的上界位数,|x0| < 2^xbits")
    ap.add_argument("--m", type=int, default=None, help="格参数 m(默认自动)")
    ap.add_argument("--t", type=int, default=None, help="格参数 t(默认 floor(d*m*(1/beta-1)))")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    coeffs = [int(c.strip(), 0) for c in args.coeffs.split(",") if c.strip()]
    roots = coppersmith_univariate(coeffs, int(args.n, 0), args.beta, 2 ** args.xbits,
                                   args.m, args.t, args.verbose)
    if not roots:
        print("未找到小根。可尝试:增大 --m(如 10~14)、确认 beta 是否为 0.5、检查 f 是否首一/根界是否过大")
        return 1
    for r in roots:
        print(f"root = {r}")
        print(f"        hex = {hex(r)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
