import torch
import unittest
from context import HybridLinearSystem


class HybridLinearSystemTest(unittest.TestCase):
    def setUp(self):
        pass

    def test_constructor(self):
        dut = HybridLinearSystem.HybridLinearSystem(3, 2, torch.float64)
        self.assertEqual(dut.x_dim, 3)
        self.assertEqual(dut.u_dim, 2)
        self.assertEqual(dut.dtype, torch.float64)
        self.assertEqual(dut.num_modes, 0)

    def test_add_mode(self):
        dut = HybridLinearSystem.HybridLinearSystem(2, 1, torch.float64)
        A0 = torch.tensor([[1, 2], [2, 1]], dtype=dut.dtype)
        B0 = torch.tensor([[2], [3]], dtype=dut.dtype)
        c0 = torch.tensor([[-1], [2]], dtype=dut.dtype)
        P0 = torch.cat((torch.eye(3, dtype=dut.dtype),
                        -torch.eye(3, dtype=dut.dtype)), dim=0)
        q0 = torch.tensor([[1], [2], [3], [1], [2], [3]], dtype=dut.dtype)
        dut.add_mode(A0, B0, c0, P0, q0)
        self.assertEqual(dut.num_modes, 1)

    def test_mixed_integer_constraints(self):
        dut = HybridLinearSystem.HybridLinearSystem(2, 1, torch.float64)
        A0 = torch.tensor([[1, 2], [2, 1]], dtype=dut.dtype)
        B0 = torch.tensor([[2], [3]], dtype=dut.dtype)
        c0 = torch.tensor([[-1], [2]], dtype=dut.dtype)
        P0 = torch.cat((torch.eye(3, dtype=dut.dtype),
                        -torch.eye(3, dtype=dut.dtype)), dim=0)
        q0 = torch.tensor([[1], [2], [3], [1], [2], [3]], dtype=dut.dtype)
        dut.add_mode(A0, B0, c0, P0, q0)
        A1 = torch.tensor([[3, 2], [-2, 1]], dtype=dut.dtype)
        B1 = torch.tensor([[-2], [4]], dtype=dut.dtype)
        c1 = torch.tensor([[3], [-2]], dtype=dut.dtype)
        P1 = torch.cat((3 * torch.eye(3, dtype=dut.dtype),
                        -2 * torch.eye(3, dtype=dut.dtype),
                        torch.tensor([[1, 2, 3]], dtype=dut.dtype)), dim=0)
        q1 = torch.tensor([[12], [2], [4], [-1], [1], [3], [7]],
                          dtype=dut.dtype)
        dut.add_mode(A1, B1, c1, P1, q1)
        A2 = torch.tensor([[3, -2], [6, 1]], dtype=dut.dtype)
        B2 = torch.tensor([[2], [7]], dtype=dut.dtype)
        c2 = torch.tensor([[1], [-4]], dtype=dut.dtype)
        P2 = torch.cat((2 * torch.eye(3, dtype=dut.dtype),
                        -5 * torch.eye(3, dtype=dut.dtype),
                        torch.tensor([[4, 2, 1]], dtype=dut.dtype)), dim=0)
        q2 = torch.tensor([[1], [3], [3], [-1], [1], [3], [4]],
                          dtype=dut.dtype)
        dut.add_mode(A2, B2, c2, P2, q2)

        x_lo = torch.tensor([[-3], [-2]], dtype=dut.dtype)
        x_up = torch.tensor([[10], [8]], dtype=dut.dtype)
        u_lo = torch.tensor([[-5]], dtype=dut.dtype)
        u_up = torch.tensor([[10]], dtype=dut.dtype)
        (Aeq_slack, Aeq_alpha, Ain_x, Ain_u, Ain_slack, Ain_alpha, rhs_in) =\
            dut.mixed_integer_constraints(x_lo, x_up, u_lo, u_up)

        def generate_xu(mode, expect_in_mode):
            # @param expect_in_mode. Do you want to generate x/u in that mode?
            is_in_mode = not expect_in_mode
            while is_in_mode != expect_in_mode:
                x = torch.empty((2, 1), dtype=dut.dtype)
                u = torch.empty((1, 1), dtype=dut.dtype)
                for i in range(dut.x_dim):
                    x[i] = torch.DoubleTensor(1, 1).\
                        uniform_(x_lo[i, 0], x_up[i, 0])[0, 0]
                for i in range(dut.u_dim):
                    u[i] = torch.DoubleTensor(1, 1).\
                        uniform_(u_lo[i, 0], u_up[i, 0])[0, 0]
                if torch.all(dut.P[mode] @ torch.cat((x, u), dim=0) <=
                             dut.q[mode]):
                    is_in_mode = True
                else:
                    is_in_mode = False
            return (x, u)

        def test_mode(mode):
            (x, u) = generate_xu(mode, True)
            # First find x and u in this mode.
            x_next = dut.A[mode] @ x + dut.B[mode] @ u + dut.c[mode]

            alpha = torch.zeros(dut.num_modes, 1, dtype=dut.dtype)
            alpha[mode][0] = 1
            s = torch.zeros(dut.num_modes * dut.x_dim, 1, dtype=dut.dtype)
            t = torch.zeros(dut.num_modes * dut.u_dim, 1, dtype=dut.dtype)
            s[dut.x_dim * mode: dut.x_dim * (mode + 1)] = x
            t[dut.u_dim * mode: dut.u_dim * (mode + 1)] = u
            slack = torch.cat((s, t), dim=0)
            self.assertTrue(
                torch.all(torch.abs(x_next - (Aeq_slack @ slack
                                              + Aeq_alpha @ alpha)) < 1E-12))
            lhs_in = Ain_x @ x + Ain_u @ u + Ain_slack @ slack\
                + Ain_alpha @ alpha
            self.assertTrue(torch.all(lhs_in <= rhs_in + 1E-12))

        for mode in range(dut.num_modes):
            test_mode(mode)
            test_mode(mode)
            test_mode(mode)

        def test_ineq(mode):
            # Randomly sample x and u. If x and u are not in that mode, then
            # there should be no slack variables such that the inequality
            # constraints are satisfied.
            (x, u) = generate_xu(mode, False)
            alpha = torch.zeros(dut.num_modes, 1, dtype=dut.dtype)
            alpha[mode] = 1
            s = torch.zeros(dut.num_modes * dut.x_dim, 1, dtype=dut.dtype)
            t = torch.zeros(dut.num_modes * dut.u_dim, 1, dtype=dut.dtype)
            s[dut.x_dim * mode: dut.x_dim * (mode + 1)] = x
            t[dut.u_dim * mode: dut.u_dim * (mode + 1)] = u
            slack = torch.cat((s, t), dim=0)
            lhs = Ain_x @ x + Ain_u @ u + Ain_slack @ slack + Ain_alpha @ alpha
            self.assertFalse(torch.all(lhs < rhs_in + 1E-12))

        for mode in range(dut.num_modes):
            test_ineq(mode)


if __name__ == "__main__":
    unittest.main()
