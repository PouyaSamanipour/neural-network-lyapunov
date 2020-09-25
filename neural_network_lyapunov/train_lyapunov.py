import torch
import torch.utils.tensorboard
import numpy as np
import gurobipy
import copy
import neural_network_lyapunov.hybrid_linear_system as hybrid_linear_system
import neural_network_lyapunov.relu_to_optimization as relu_to_optimization
import neural_network_lyapunov.train_utils as train_utils
import neural_network_lyapunov.line_search_gd as line_search_gd
import neural_network_lyapunov.line_search_adam as line_search_adam

from enum import Enum


# We want to minimize two losses to 0, one is the violation of the Lyapunov
# positivity constraint, the second is the violation of the Lyapunov derivative
# constraint. The gradient of these two losses might be in opposite directions,
# so minimizing one loss might increase the other loss. Hence we consider to
# project the gradient of one loss to the null space of another loss. If we
# take a small step along this projected gradient, it should not decrease one
# loss but do not affect the other.
# We provide 4 methods
# 1. No projection, just use the sum of the gradient.
# 2. Use the sum of the projected gradient.
# 3. Alternating between the two projected gradient. In one iteration decrease
#    one loss, in the next iteation decrease the other loss.
# 4. Emphasize on positivity loss, we project the derivative loss gradient to
#    the nullspace of the positivity gradient, add this projected gradient to
#    the positivity loss gradient.
class ProjectGradientMethod(Enum):
    # Do not project the gradient
    NONE = 1
    SUM = 2
    ALTERNATE = 3
    EMPHASIZE_POSITIVITY = 4


class TrainLyapunovReLU:
    """
    We will train a ReLU network, such that the function
    V(x) = ReLU(x) - ReLU(x*) + λ|x-x*|₁ is a Lyapunov function that certifies
    (exponential) convergence. Namely V(x) should satisfy the following
    conditions
    1. V(x) > 0 ∀ x ≠ x*
    2. dV(x) ≤ -ε V(x) ∀ x
    where dV(x) = V̇(x) for continuous time system, and
    dV(x[n]) = V(x[n+1]) - V(x[n]) for discrete time system.
    In order to find such V(x), we penalize a (weighted) sum of the following
    loss
    1. hinge(-V(xⁱ)) for sampled state xⁱ.
    2. hinge(dV(xⁱ) + ε V(xⁱ)) for sampled state xⁱ.
    3. -min_x V(x) - ε₂ |x - x*|₁
    4. max_x dV(x) + ε V(x)
    where ε₂ is a given positive scalar, and |x - x*|₁ is the 1-norm of x - x*.
    hinge(z) = max(z + margin, 0) where margin is a given scalar.
    """

    def __init__(self, lyapunov_hybrid_system, V_lambda, x_equilibrium):
        """
        @param lyapunov_hybrid_system This input should define a common
        interface
        lyapunov_positivity_as_milp() (which represents
        minₓ V(x) - ε₂ |x - x*|₁)
        lyapunov_positivity_loss_at_samples() (which represents
        mean(hinge(-V(xⁱ))))
        lyapunov_derivative_as_milp() (which represents maxₓ dV(x) + ε V(x))
        lyapunov_derivative_loss_at_samples() (which represents
        mean(hinge(dV(xⁱ) + ε V(xⁱ))).
        One example of input type is lyapunov.LyapunovDiscreteTimeHybridSystem.
        @param V_lambda λ in the documentation above.
        @param x_equilibrium The equilibrium state.
        """
        self.lyapunov_hybrid_system = lyapunov_hybrid_system
        assert(isinstance(V_lambda, float))
        self.V_lambda = V_lambda
        assert(isinstance(x_equilibrium, torch.Tensor))
        assert(x_equilibrium.shape == (lyapunov_hybrid_system.system.x_dim,))
        self.x_equilibrium = x_equilibrium
        # The learning rate of the optimizer
        self.learning_rate = 0.003
        # Number of iterations in the training.
        self.max_iterations = 1000
        # The weight of hinge(-V(xⁱ)) in the total loss
        self.lyapunov_positivity_sample_cost_weight = 1.
        # The margin in hinge(-V(xⁱ))
        self.lyapunov_positivity_sample_margin = 0.
        # The weight of hinge(dV(xⁱ) + ε V(xⁱ)) in the total loss
        self.lyapunov_derivative_sample_cost_weight = 1.
        # The margin in hinge(dV(xⁱ) + ε V(xⁱ))
        self.lyapunov_derivative_sample_margin = 0.
        # The weight of -min_x V(x) - ε₂ |x - x*|₁ in the total loss
        self.lyapunov_positivity_mip_cost_weight = 10.
        # The number of (sub)optimal solutions for the MIP
        # min_x V(x) - V(x*) - ε₂ |x - x*|₁
        self.lyapunov_positivity_mip_pool_solutions = 10
        # The weight of max_x dV(x) + εV(x)
        self.lyapunov_derivative_mip_cost_weight = 10.
        # The number of (sub)optimal solutions for the MIP
        # max_x dV(x) + εV(x)
        self.lyapunov_derivative_mip_pool_solutions = 10
        # If set to true, we will print some messages during training.
        self.output_flag = False
        # The MIP objective loss is ∑ⱼ rʲ * j_th_objective. r is the decay
        # rate.
        self.lyapunov_positivity_mip_cost_decay_rate = 0.9
        self.lyapunov_derivative_mip_cost_decay_rate = 0.9
        # This is ε₂ in  V(x) >=  ε₂ |x - x*|₁
        self.lyapunov_positivity_epsilon = 0.01
        # This is ε in dV(x) ≤ -ε V(x)
        self.lyapunov_derivative_epsilon = 0.01

        # The convergence tolerance for the training.
        # When the lyapunov function
        # minₓ V(x)-ε₂|x-x*|₁ > -lyapunov_positivity_convergence_tol
        # and the lyapunov derivative satisfies
        # maxₓ dV(x) + εV(x) < lyapunov_derivative_convergence_tol
        # we regard that the Lyapunov function is found.
        self.lyapunov_positivity_convergence_tol = 1E-6
        self.lyapunov_derivative_convergence_tol = 3e-5

        # We support Adam or SGD.
        self.optimizer = "Adam"

        self.project_gradient_method = ProjectGradientMethod.NONE

        # If summary writer is not None, then we use tensorboard to write
        # training loss to the summary writer.
        self.summary_writer_folder = None

        # parameter used in SGD
        self.momentum = 0.
        # parameter used in line search optimizer
        self.loss_minimal_decrement = 0.

        # Whether we add the adversarial states to the training set.
        self.add_adversarial_state_to_training = False
        # We compute the sample loss on the most recent max_sample_pool_size
        # samples.
        self.max_sample_pool_size = 500

    def total_loss(
            self, positivity_state_samples, derivative_state_samples,
            derivative_state_samples_next,
            lyapunov_positivity_sample_cost_weight=None,
            lyapunov_derivative_sample_cost_weight=None,
            lyapunov_positivity_mip_cost_weight=None,
            lyapunov_derivative_mip_cost_weight=None):
        """
        Compute the total loss as the summation of
        1. hinge(-V(xⁱ) + ε₂ |xⁱ - x*|₁) for sampled state xⁱ.
        2. hinge(dV(xⁱ) + ε V(xⁱ)) for sampled state xⁱ.
        3. -min_x V(x) - ε₂ |x - x*|₁
        4. max_x dV(x) + ε V(x)
        @param positivity_state_samples All sample states on which we compute
        the violation of Lyapunov positivity constraint.
        @param derivative_state_samples All sample states on which we compute
        the violation of Lyapunov derivative constraint.
        @param derivative_state_samples_next. The next state(s) of the sampled
        state. derivative_state_samples_next contains next state(s) of
        derivative_state_samples[i].
        @param loss, positivity_mip_objective, derivative_mip_objective,
        positivity_sample_loss, derivative_sample_loss, positivity_mip_loss,
        derivative_mip_loss
        positivity_mip_objective is the objective value
        min_x V(x) - ε₂ |x - x*|₁. We want this value to be non-negative.
        derivative_mip_objective is the objective value max_x dV(x) + ε V(x).
        We want this value to be non-positive.
        positivity_sample_loss is weight * cost1
        derivative_sample_loss is weight * cost2
        positivity_mip_loss is weight * cost3
        derivative_mip_loss is weight * cost4
        """
        assert(isinstance(positivity_state_samples, torch.Tensor))
        assert(isinstance(derivative_state_samples, torch.Tensor))
        assert(isinstance(derivative_state_samples_next, torch.Tensor))
        assert(
            positivity_state_samples.shape[1] ==
            self.lyapunov_hybrid_system.system.x_dim)
        assert(
            derivative_state_samples.shape[1] ==
            self.lyapunov_hybrid_system.system.x_dim)
        assert(
            derivative_state_samples_next.shape ==
            (derivative_state_samples.shape[0],
             self.lyapunov_hybrid_system.system.x_dim))
        dtype = self.lyapunov_hybrid_system.system.dtype
        lyapunov_positivity_as_milp_return = self.lyapunov_hybrid_system.\
            lyapunov_positivity_as_milp(
                self.x_equilibrium, self.V_lambda,
                self.lyapunov_positivity_epsilon)
        lyapunov_positivity_mip = lyapunov_positivity_as_milp_return[0]
        lyapunov_positivity_mip.gurobi_model.setParam(
            gurobipy.GRB.Param.OutputFlag, False)
        if self.lyapunov_positivity_mip_pool_solutions > 1:
            lyapunov_positivity_mip.gurobi_model.setParam(
                gurobipy.GRB.Param.PoolSearchMode, 2)
            lyapunov_positivity_mip.gurobi_model.setParam(
                gurobipy.GRB.Param.PoolSolutions,
                self.lyapunov_positivity_mip_pool_solutions)
        lyapunov_positivity_mip.gurobi_model.optimize()

        lyapunov_derivative_as_milp_return = self.lyapunov_hybrid_system.\
            lyapunov_derivative_as_milp(
                self.x_equilibrium, self.V_lambda,
                self.lyapunov_derivative_epsilon)
        lyapunov_derivative_mip = lyapunov_derivative_as_milp_return[0]
        lyapunov_derivative_mip.gurobi_model.setParam(
            gurobipy.GRB.Param.OutputFlag, False)
        if (self.lyapunov_derivative_mip_pool_solutions > 1):
            lyapunov_derivative_mip.gurobi_model.setParam(
                gurobipy.GRB.Param.PoolSearchMode, 2)
            lyapunov_derivative_mip.gurobi_model.setParam(
                gurobipy.GRB.Param.PoolSolutions,
                self.lyapunov_derivative_mip_pool_solutions)
        lyapunov_derivative_mip.gurobi_model.optimize()

        relu_zeta_val = np.array([
            np.round(v.x) for v in lyapunov_derivative_as_milp_return[2]])
        relu_activation_pattern = relu_to_optimization.\
            relu_activation_binary_to_pattern(
                self.lyapunov_hybrid_system.lyapunov_relu, relu_zeta_val)
        relu_gradient, _, _, _ = relu_to_optimization.\
            ReLUGivenActivationPattern(
                self.lyapunov_hybrid_system.lyapunov_relu,
                self.lyapunov_hybrid_system.system.x_dim,
                relu_activation_pattern,
                self.lyapunov_hybrid_system.system.dtype)
        if self.output_flag:
            print(f"relu gradient {relu_gradient.squeeze().detach().numpy()}")
            print("lyapunov derivative MIP Relu activation: "
                  f"{np.argwhere(relu_zeta_val == 1).squeeze()}")
            print("adversarial x " +
                  f"{[v.x for v in lyapunov_derivative_as_milp_return[1]]}")

        loss = torch.tensor(0., dtype=dtype)
        relu_at_equilibrium = \
            self.lyapunov_hybrid_system.lyapunov_relu.forward(
                self.x_equilibrium)

        def set_weight(val, default_val):
            return val if val is not None else default_val
        lyapunov_positivity_sample_cost_weight = set_weight(
            lyapunov_positivity_sample_cost_weight,
            self.lyapunov_positivity_sample_cost_weight)
        if lyapunov_positivity_sample_cost_weight != 0 and\
                positivity_state_samples.shape[0] > 0:
            if positivity_state_samples.shape[0] > self.max_sample_pool_size:
                positivity_state_samples_in_pool = \
                    positivity_state_samples[-self.max_sample_pool_size:]
            else:
                positivity_state_samples_in_pool = positivity_state_samples
            positivity_sample_loss = lyapunov_positivity_sample_cost_weight *\
                self.lyapunov_hybrid_system.\
                lyapunov_positivity_loss_at_samples(
                    relu_at_equilibrium, self.x_equilibrium,
                    positivity_state_samples_in_pool, self.V_lambda,
                    self.lyapunov_positivity_epsilon,
                    margin=self.lyapunov_positivity_sample_margin)
        else:
            positivity_sample_loss = 0.
        lyapunov_derivative_sample_cost_weight = set_weight(
            lyapunov_derivative_sample_cost_weight,
            self.lyapunov_derivative_sample_cost_weight)
        if lyapunov_derivative_sample_cost_weight != 0 and\
                derivative_state_samples.shape[0] > 0:
            if derivative_state_samples.shape[0] > self.max_sample_pool_size:
                derivative_state_samples_in_pool = \
                    derivative_state_samples[-self.max_sample_pool_size:]
                derivative_state_samples_next_in_pool = \
                    derivative_state_samples_next[-self.max_sample_pool_size:]
            else:
                derivative_state_samples_in_pool = derivative_state_samples
                derivative_state_samples_next_in_pool = \
                    derivative_state_samples_next
            derivative_sample_loss = lyapunov_derivative_sample_cost_weight *\
                self.lyapunov_hybrid_system.\
                lyapunov_derivative_loss_at_samples_and_next_states(
                    self.V_lambda, self.lyapunov_derivative_epsilon,
                    derivative_state_samples_in_pool,
                    derivative_state_samples_next_in_pool, self.x_equilibrium,
                    margin=self.lyapunov_derivative_sample_margin)
        else:
            derivative_sample_loss = 0.

        lyapunov_positivity_mip_cost_weight = set_weight(
            lyapunov_positivity_mip_cost_weight,
            self.lyapunov_positivity_mip_cost_weight)
        positivity_mip_loss = 0.
        if lyapunov_positivity_mip_cost_weight != 0:
            for mip_sol_number in range(
                    self.lyapunov_positivity_mip_pool_solutions):
                if mip_sol_number < \
                        lyapunov_positivity_mip.gurobi_model.solCount:
                    positivity_mip_loss += \
                        lyapunov_positivity_mip_cost_weight * \
                        torch.pow(torch.tensor(
                            self.lyapunov_positivity_mip_cost_decay_rate,
                            dtype=dtype), mip_sol_number) *\
                        -lyapunov_positivity_mip.\
                        compute_objective_from_mip_data_and_solution(
                            solution_number=mip_sol_number, penalty=1e-13)
        lyapunov_derivative_mip_cost_weight = set_weight(
            lyapunov_derivative_mip_cost_weight,
            self.lyapunov_derivative_mip_cost_weight)
        derivative_mip_loss = 0.
        if lyapunov_derivative_mip_cost_weight != 0:
            for mip_sol_number in range(
                    self.lyapunov_derivative_mip_pool_solutions):
                if (mip_sol_number <
                        lyapunov_derivative_mip.gurobi_model.solCount):
                    mip_cost = lyapunov_derivative_mip.\
                        compute_objective_from_mip_data_and_solution(
                            solution_number=mip_sol_number, penalty=1e-13)
                    derivative_mip_loss += \
                        lyapunov_derivative_mip_cost_weight *\
                        torch.pow(torch.tensor(
                            self.lyapunov_derivative_mip_cost_decay_rate,
                            dtype=dtype), mip_sol_number) * mip_cost
                    lyapunov_derivative_mip.gurobi_model.setParam(
                        gurobipy.GRB.Param.SolutionNumber, mip_sol_number)
        loss = positivity_sample_loss + derivative_sample_loss + \
            positivity_mip_loss + derivative_mip_loss

        # We add the most adverisal states of the positivity MIP and derivative
        # MIP to the training set. Note we solve positivity MIP and derivative
        # MIP separately. This is different from adding the most adversarial
        # state of the total loss.
        if self.add_adversarial_state_to_training:
            positivity_mip_adversarial = torch.tensor([
                v.x for v in lyapunov_positivity_as_milp_return[1]],
                dtype=self.lyapunov_hybrid_system.system.dtype)
            derivative_mip_adversarial = torch.tensor([
                v.x for v in lyapunov_derivative_as_milp_return[1]],
                dtype=self.lyapunov_hybrid_system.system.dtype)
            derivative_mip_adversarial_mode = np.argwhere(np.array(
                [v.x for v in lyapunov_derivative_as_milp_return[3]])
                > 0.99)[0][0]
            derivative_mip_adversarial_next = self.lyapunov_hybrid_system.\
                system.step_forward(
                    derivative_mip_adversarial,
                    derivative_mip_adversarial_mode)
            positivity_state_samples = torch.cat(
                [positivity_state_samples,
                 positivity_mip_adversarial.unsqueeze(0)], dim=0)
            derivative_state_samples = torch.cat(
                [derivative_state_samples,
                 derivative_mip_adversarial.unsqueeze(0)], dim=0)
            derivative_state_samples_next = torch.cat(
                [derivative_state_samples_next,
                 derivative_mip_adversarial_next.unsqueeze(0)], dim=0)

        return loss, lyapunov_positivity_mip.gurobi_model.ObjVal,\
            lyapunov_derivative_mip.gurobi_model.ObjVal,\
            positivity_sample_loss, derivative_sample_loss,\
            positivity_mip_loss, derivative_mip_loss,\
            positivity_state_samples, derivative_state_samples,\
            derivative_state_samples_next

    def train_with_line_search(self, state_samples_all):
        assert(isinstance(state_samples_all, torch.Tensor))
        assert(
            state_samples_all.shape[1] ==
            self.lyapunov_hybrid_system.system.x_dim)
        positivity_state_samples = state_samples_all.clone()
        derivative_state_samples = state_samples_all.clone()
        derivative_state_samples_next = torch.stack([
            self.lyapunov_hybrid_system.system.step_forward(
                derivative_state_samples[i]) for i in
            range(derivative_state_samples.shape[0])], dim=0)
        lyapunov_positivity_mip_costs = [None] * self.max_iterations
        lyapunov_derivative_mip_costs = [None] * self.max_iterations
        losses = [None] * self.max_iterations
        if self.summary_writer_folder is not None:
            writer = torch.utils.tensorboard.SummaryWriter(
                self.summary_writer_folder)

        iter_count = 0

        def closure():
            self.lyapunov_hybrid_system.lyapunov_relu.zero_grad()
            loss, lyapunov_positivity_mip_costs[iter_count],\
                lyapunov_derivative_mip_costs[iter_count], _, _, _, _, _, _, _\
                = self.total_loss(
                    positivity_state_samples, derivative_state_samples,
                    derivative_state_samples_next)
            print("derivative mip loss " +
                  f"{lyapunov_derivative_mip_costs[iter_count]}")
            return loss

        relu_params = [None] * self.max_iterations

        loss = closure()
        loss.backward()
        losses[0] = loss.item()
        relu_params[iter_count] = torch.cat(
            [p.data.reshape((-1,)) for p in
             self.lyapunov_hybrid_system.lyapunov_relu.parameters()])
        iter_count = 1
        if self.optimizer == "GD":
            optimizer = line_search_gd.LineSearchGD(
                self.lyapunov_hybrid_system.lyapunov_relu.parameters(),
                lr=self.learning_rate,
                momentum=self.momentum, min_step_size_decrease=1e-4,
                loss_minimal_decrement=self.loss_minimal_decrement,
                step_size_reduction=0.2, min_improvement=self.min_improvement)
        elif self.optimizer == "LineSearchAdam":
            optimizer = line_search_adam.LineSearchAdam(
                self.lyapunov_hybrid_system.lyapunov_relu.parameters(),
                lr=self.learning_rate,
                min_step_size_decrease=1e-4,
                loss_minimal_decrement=self.loss_minimal_decrement,
                step_size_reduction=0.2, min_improvement=self.min_improvement)
        while iter_count < self.max_iterations:
            loss = optimizer.step(closure, losses[iter_count-1])
            loss.backward()
            losses[iter_count] = loss.item()
            relu_params[iter_count] = torch.cat(
                [p.data.reshape((-1,)) for p in
                 self.lyapunov_hybrid_system.lyapunov_relu.parameters()])
            if lyapunov_positivity_mip_costs[iter_count] < \
                self.lyapunov_positivity_convergence_tol and \
                lyapunov_derivative_mip_costs[iter_count] < \
                    self.lyapunov_derivative_convergence_tol:
                return True
            print(f"iter {iter_count}, loss {losses[iter_count]}," +
                  "positivity mip cost " +
                  f"{lyapunov_positivity_mip_costs[iter_count]}, derivative " +
                  f"mip cost {lyapunov_derivative_mip_costs[iter_count]}\n")
            if self.summary_writer_folder is not None:
                writer.add_scalar(
                    "loss", losses[iter_count], iter_count)
                writer.add_scalar(
                    "positivity MIP cost",
                    lyapunov_positivity_mip_costs[iter_count], iter_count)
                writer.add_scalar(
                    "derivative MIP cost",
                    lyapunov_derivative_mip_costs[iter_count], iter_count)
            iter_count += 1
        return False

    def train(self, state_samples_all):
        assert(isinstance(state_samples_all, torch.Tensor))
        assert(
            state_samples_all.shape[1] ==
            self.lyapunov_hybrid_system.system.x_dim)
        positivity_state_samples = state_samples_all.clone()
        derivative_state_samples = state_samples_all.clone()
        derivative_state_samples_next = torch.stack([
            self.lyapunov_hybrid_system.system.step_forward(
                derivative_state_samples[i]) for i in
            range(derivative_state_samples.shape[0])], dim=0)
        iter_count = 0
        if self.optimizer == "Adam":
            optimizer = torch.optim.Adam(
                self.lyapunov_hybrid_system.lyapunov_relu.parameters(),
                lr=self.learning_rate)
        elif self.optimizer == "SGD":
            optimizer = torch.optim.SGD(
                self.lyapunov_hybrid_system.lyapunov_relu.parameters(),
                lr=self.learning_rate,
                momentum=self.momentum)
        else:
            raise Exception(
                "train: unknown optimizer, only support Adam or SGD.")
        lyapunov_positivity_mip_costs = [None] * self.max_iterations
        lyapunov_derivative_mip_costs = [None] * self.max_iterations
        losses = [None] * self.max_iterations
        if self.summary_writer_folder is not None:
            writer = torch.utils.tensorboard.SummaryWriter(
                self.summary_writer_folder)
        relu_params = [None] * self.max_iterations
        gradients = [None] * self.max_iterations
        while iter_count < self.max_iterations:
            optimizer.zero_grad()
            loss, lyapunov_positivity_mip_costs[iter_count],\
                lyapunov_derivative_mip_costs[iter_count], \
                positivity_sample_loss, derivative_sample_loss,\
                positivity_mip_loss, derivative_mip_loss,\
                positivity_state_samples, derivative_state_samples,\
                derivative_state_samples_next\
                = self.total_loss(
                    positivity_state_samples, derivative_state_samples,
                    derivative_state_samples_next)
            losses[iter_count] = loss.item()

            if self.summary_writer_folder is not None:
                writer.add_scalar("loss", loss.item(), iter_count)
                writer.add_scalar(
                    "positivity MIP cost",
                    lyapunov_positivity_mip_costs[iter_count], iter_count)
                writer.add_scalar(
                    "derivative MIP cost",
                    lyapunov_derivative_mip_costs[iter_count], iter_count)
            if self.output_flag:
                print(f"Iter {iter_count}, loss {loss}, " +
                      "positivity cost " +
                      f"{lyapunov_positivity_mip_costs[iter_count]}, " +
                      "derivative_cost " +
                      f"{lyapunov_derivative_mip_costs[iter_count]}")
            if lyapunov_positivity_mip_costs[iter_count] >=\
                -self.lyapunov_positivity_convergence_tol and\
                lyapunov_derivative_mip_costs[iter_count] <= \
                    self.lyapunov_derivative_convergence_tol:
                return (True, losses[:iter_count+1],
                        lyapunov_positivity_mip_costs[:iter_count+1],
                        lyapunov_derivative_mip_costs[:iter_count+1])
            if self.project_gradient_method == ProjectGradientMethod.SUM:
                project_gradient_mode = train_utils.ProjectGradientMode.BOTH
            elif self.project_gradient_method == \
                    ProjectGradientMethod.ALTERNATE:
                if iter_count == 0:
                    project_gradient_mode = train_utils.ProjectGradientMode.\
                        LOSS2
                else:
                    project_gradient_mode = train_utils.ProjectGradientMode.\
                        LOSS1 if project_gradient_mode == \
                        train_utils.ProjectGradientMode.LOSS2 else \
                        train_utils.ProjectGradientMode.LOSS2
            elif self.project_gradient_method == \
                    ProjectGradientMethod.EMPHASIZE_POSITIVITY:
                project_gradient_mode = train_utils.ProjectGradientMode.\
                    EMPHASIZE_LOSS1
            if self.project_gradient_method == ProjectGradientMethod.NONE:
                loss.backward()
            else:
                (need_projection, n1, n2) = train_utils.project_gradient(
                    self.lyapunov_hybrid_system.lyapunov_relu,
                    positivity_sample_loss + positivity_mip_loss,
                    derivative_sample_loss + derivative_mip_loss,
                    project_gradient_mode, retain_graph=False)
            relu_params[iter_count] = torch.cat(
                [p.data.reshape((-1,)) for p in
                 self.lyapunov_hybrid_system.lyapunov_relu.parameters()])
            gradients[iter_count] = torch.cat(
                [p.grad.reshape((-1,)) for p in
                 self.lyapunov_hybrid_system.lyapunov_relu.parameters()])
            optimizer.step()
            iter_count += 1
        return (False, losses, lyapunov_positivity_mip_costs,
                lyapunov_derivative_mip_costs)

    def train_lyapunov_on_samples(
        self, state_samples_all, max_iterations,
            max_increasing_iterations):
        """
        Train a ReLU network on given state samples (not the adversarial states
        found by MIP). The loss function is the weighted sum of the lyapunov
        positivity condition violation and the lyapunov derivative condition
        violation on these samples. We stop the training when either the
        maximum iteration is reached, or when many consecutive iterations the
        MIP costs keeps increasing (which means the network overfits to the
        training data). Return the best network (the one with the minimal
        MIP loss) found so far.
        @param state_samples_all A torch tensor, state_samples_all[i] is the
        i'th sample
        @param max_iterations The maximal number of iterations.
        @param max_increasing_iterations If the MIP loss keeps increasing for
        this number of iterations, stop the training.
        """
        assert(isinstance(state_samples_all, torch.Tensor))
        assert(
            state_samples_all.shape[1] ==
            self.lyapunov_hybrid_system.system.x_dim)
        state_samples_next = torch.stack([
            self.lyapunov_hybrid_system.system.step_forward(
                state_samples_all[i]) for i in
            range(state_samples_all.shape[0])], dim=0)
        best_loss = np.inf
        num_increasing_iterations = 0
        optimizer = torch.optim.Adam(
            self.lyapunov_hybrid_system.lyapunov_relu.parameters(),
            lr=self.learning_rate)
        previous_mip_loss = np.inf
        for iter_count in range(max_iterations):
            optimizer.zero_grad()
            loss, lyapunov_positivity_mip_cost, lyapunov_derivative_mip_cost, \
                _, _, _, _, _, _, _\
                = self.total_loss(
                    state_samples_all, state_samples_all,
                    state_samples_next,
                    lyapunov_positivity_mip_cost_weight=0.,
                    lyapunov_derivative_mip_cost_weight=0.)
            mip_loss = self.lyapunov_positivity_mip_cost_weight * \
                -lyapunov_positivity_mip_cost + \
                self.lyapunov_derivative_mip_cost_weight * \
                lyapunov_derivative_mip_cost
            if mip_loss < best_loss:
                best_loss = mip_loss
                best_relu = copy.deepcopy(
                    self.lyapunov_hybrid_system.lyapunov_relu)
            if mip_loss > previous_mip_loss:
                num_increasing_iterations += 1
                if num_increasing_iterations == max_increasing_iterations:
                    print(f"best loss {best_loss}")
                    break
            else:
                # reset the counter.
                num_increasing_iterations = 0
            previous_mip_loss = mip_loss
            if self.output_flag:
                print(f"Iter {iter_count}, loss {loss}, " +
                      "positivity cost " +
                      f"{lyapunov_positivity_mip_cost}, " +
                      "derivative_cost " +
                      f"{lyapunov_derivative_mip_cost}, " +
                      f"mip loss {mip_loss}\n")
            loss.backward()
            optimizer.step()
        print(f"best loss {best_loss}")
        self.lyapunov_hybrid_system.lyapunov_relu.load_state_dict(
            best_relu.state_dict())


class TrainValueApproximator:
    """
    Given a piecewise affine system and some sampled initial state, compute the
    cost-to-go for these sampled states, and then train a network to
    approximate the cost-to-go, such that
    network(x) - network(x*) + λ|x-x*|₁ ≈ cost_to_go(x)
    """
    def __init__(self):
        self.max_epochs = 100
        self.convergence_tolerance = 1e-3
        self.learning_rate = 0.02

    def train_with_cost_to_go(
            self, network, x0_value_samples, V_lambda, x_equilibrium):
        """
        Similar to train() function, but with given samples on initial_state
        and cost-to-go.
        """
        state_samples_all = torch.stack([
            pair[0] for pair in x0_value_samples], dim=0)
        value_samples_all = torch.stack([pair[1] for pair in x0_value_samples])
        optimizer = torch.optim.Adam(
            network.parameters(), lr=self.learning_rate)
        for epoch in range(self.max_epochs):
            optimizer.zero_grad()
            relu_output = network(state_samples_all)
            relu_x_equilibrium = network.forward(x_equilibrium)
            value_relu = relu_output.squeeze() - relu_x_equilibrium +\
                V_lambda * torch.norm(
                    state_samples_all - x_equilibrium.reshape((1, -1)).
                    expand(state_samples_all.shape[0], -1), dim=1, p=1)
            loss = torch.nn.MSELoss()(value_relu, value_samples_all)
            if (loss.item() <= self.convergence_tolerance):
                return True, loss.item()
            loss.backward()
            optimizer.step()
        return False, loss.item()

    def train(
        self, system, network, V_lambda, x_equilibrium, instantaneous_cost_fun,
            x0_samples, T, discrete_time_flag, x_goal=None, pruner=None):
        """
        Train a network such that
        network(x) - network(x*) + λ*|x-x*|₁ ≈ cost_to_go(x)
        @param system An AutonomousHybridLinearSystem instance.
        @param network a pytorch neural network.
        @param V_lambda λ.
        @param x_equilibrium x*.
        @param instantaneous_cost_fun A callable to evaluate the instantaneous
        cost for a state.
        @param x0_samples A list of torch tensors. x0_samples[i] is the i'th
        sampled x0.
        @param T The horizon to evaluate the cost-to-go. For a discrete time
        system, T must be an integer, for a continuous time system, T must be a
        float.
        @param discrete_time_flag Whether the hybrid linear system is discrete
        or continuous time.
        @param x_goal The goal position to stop simulating the trajectory when
        computing cost-to-go. Check compute_continuous_time_system_cost_to_go()
        and compute_discrete_time_system_cost_to_go() for more details.
        @param pruner A callable to decide whether to keep a state in the
        training set or not. Check generate_cost_to_go_samples() for more
        details.
        """
        assert(isinstance(
            system, hybrid_linear_system.AutonomousHybridLinearSystem))
        assert(isinstance(x_equilibrium, torch.Tensor))
        assert(x_equilibrium.shape == (system.x_dim,))
        assert(isinstance(V_lambda, float))
        assert(isinstance(x0_samples, torch.Tensor))
        x0_value_samples = hybrid_linear_system.generate_cost_to_go_samples(
            system, [x0_samples[i] for i in range(x0_samples.shape[0])], T,
            instantaneous_cost_fun, discrete_time_flag, x_goal, pruner)
        return self.train_with_cost_to_go(
            network, x0_value_samples, V_lambda, x_equilibrium)