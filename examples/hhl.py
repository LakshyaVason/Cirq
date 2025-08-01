# pylint: disable=wrong-or-nonexistent-copyright-notice
"""Demonstrates the algorithm for solving linear systems by Harrow, Hassidim, Lloyd (HHL).

The HHL algorithm solves a system of linear equations, specifically equations of the form Ax = b,
where A is a Hermitian matrix, b is a known vector, and x is the unknown vector. To solve on a
quantum system, b must be rescaled to have magnitude 1, and the equation becomes:

|x> = A**-1 |b> / || A**-1 |b> ||

The algorithm uses 3 sets of qubits: a single ancilla qubit, a register (to store eigenvalues of
A), and memory qubits (to store |b> and |x>). The following are performed in order:
1) Quantum phase estimation to extract eigenvalues of A
2) Controlled rotations of ancilla qubit
3) Uncomputation with inverse quantum phase estimation

For details about the algorithm, please refer to papers in the REFERENCE section below. The
following description uses variables defined in the HHL paper.

This example is an implementation of the HHL algorithm for arbitrary 2x2 Hermitian matrices. The
output of the algorithm are the expectation values of Pauli observables of |x>. Note that the
accuracy of the result depends on the following factors:
* Register size
* Choice of parameters C and t

The result is perfect if
* Each eigenvalue of the matrix is in the form

  2π/t * k/N,

  where 0≤k<N, and N=2^n, where n is the register size. In other words, k is a value that can be
  represented exactly by the register.
* C ≤ 2π/t * 1/N, the smallest eigenvalue that can be stored in the circuit.

The result is good if the register size is large enough such that for every pair of eigenvalues,
the ratio can be approximated by a pair of possible register values. Let s be the scaling factor
from possible register values to eigenvalues. One way to set t is

t = 2π/(sN)

For arbitrary matrices, because properties of their eigenvalues are typically unknown, parameters C
and t are fine-tuned based on their condition number.


=== REFERENCE ===
Harrow, Aram W. et al. Quantum algorithm for solving linear systems of
equations (the HHL paper)
https://arxiv.org/abs/0811.3171

Coles, Eidenbenz et al. Quantum Algorithm Implementations for Beginners
https://arxiv.org/abs/1804.03719

Brassard, Gilles et al. Quantum Amplitude Amplification and Estimation
https://arxiv.org/abs/quant-ph/0005055

Kaye, Phillip et al. An Introduction to Quantum Computing,
Section 8.4: Searching without knowing the success probability

=== CIRCUIT ===
Example of circuit with 2 register qubits.

(0, 0): ─────────────────────────Ry(θ₄)─Ry(θ₁)─Ry(θ₂)─Ry(θ₃)──────────────M──
                     ┌──────┐    │      │      │      │ ┌───┐
(1, 0): ─H─@─────────│      │──X─@──────@────X─@──────@─│   │─────────@─H────
           │         │QFT^-1│    │      │      │      │ │QFT│         │
(2, 0): ─H─┼─────@───│      │──X─@────X─@────X─@────X─@─│   │─@───────┼─H────
           │     │   └──────┘                           └───┘ │       │
(3, 0): ───e^iAt─e^2iAt───────────────────────────────────────e^-2iAt─e^-iAt─

Note: QFT in the above diagram omits swaps, which are included implicitly by
reversing qubit order for phase kickbacks.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from typing import Any

import numpy as np
import sympy

import cirq


class PhaseEstimation(cirq.Gate):
    """A gate for Quantum Phase Estimation.

    The last qubit stores the eigenvector; all other qubits store the estimated phase,
    in big-endian.

    Args:
        num_qubits: The number of qubits of the unitary.
        unitary: The unitary gate whose phases will be estimated.
    """

    def __init__(self, num_qubits, unitary):
        self._num_qubits = num_qubits
        self.U = unitary

    def num_qubits(self):
        return self._num_qubits

    def _decompose_(self, qubits):
        qubits = list(qubits)
        yield cirq.H.on_each(*qubits[:-1])
        yield PhaseKickback(self.num_qubits(), self.U)(*qubits)
        yield cirq.qft(*qubits[:-1], without_reverse=True) ** -1


class HamiltonianSimulation(cirq.EigenGate):
    """A gate that represents e^iAt.

    This EigenGate + np.linalg.eigh() implementation is used here purely for demonstrative
    purposes. If a large matrix is used, the circuit should implement actual Hamiltonian
    simulation, by using the linear operators framework in Cirq, for example.
    """

    def __init__(self, A, t, exponent=1.0):
        cirq.EigenGate.__init__(self, exponent=exponent)
        self.A = A
        self.t = t
        ws, vs = np.linalg.eigh(A)
        self.eigen_components = []
        for w, v in zip(ws, vs.T):
            theta = w * t / math.pi
            P = np.outer(v, np.conj(v))
            self.eigen_components.append((theta, P))

    def _num_qubits_(self) -> int:
        return 1

    def _with_exponent(self, exponent):
        return HamiltonianSimulation(self.A, self.t, exponent)

    def _eigen_components(self):
        return self.eigen_components


class PhaseKickback(cirq.Gate):
    """A gate for the phase kickback stage of Quantum Phase Estimation.

    It consists of a series of controlled e^iAt gates with the memory qubit as the target and
    each register qubit as the control, raised to the power of 2 based on the qubit index.
    unitary is the unitary gate whose phases will be estimated.
    """

    def __init__(self, num_qubits, unitary):
        self._num_qubits = num_qubits
        self.U = unitary

    def num_qubits(self):
        return self._num_qubits

    def _decompose_(self, qubits):
        qubits = list(qubits)
        memory = qubits.pop()
        for i, qubit in enumerate(qubits):
            yield cirq.ControlledGate(self.U ** (2**i))(qubit, memory)


class EigenRotation(cirq.Gate):
    """Perform a rotation on an ancilla equivalent to division by eigenvalues of a matrix.

    EigenRotation performs the set of rotation on the ancilla qubit equivalent to division on the
    memory register by each eigenvalue of the matrix. The last qubit is the ancilla qubit; all
    remaining qubits are the register, assumed to be big-endian.

    It consists of a controlled ancilla qubit rotation for each possible value that can be
    represented by the register. Each rotation is a Ry gate where the angle is calculated from
    the eigenvalue corresponding to the register value, up to a normalization factor C.
    """

    def __init__(self, num_qubits, C, t):
        self._num_qubits = num_qubits
        self.C = C
        self.t = t
        self.N = 2 ** (num_qubits - 1)

    def num_qubits(self):
        return self._num_qubits

    def _decompose_(self, qubits):
        for k in range(self.N):
            kGate = self._ancilla_rotation(k)

            # xor's 1 bits correspond to X gate positions.
            xor = k ^ (k - 1)

            for q in qubits[-2::-1]:
                # Place X gates
                if xor % 2 == 1:
                    yield cirq.X(q)
                xor >>= 1

                # Build controlled ancilla rotation
                kGate = cirq.ControlledGate(kGate)

            yield kGate(*qubits)

    def _ancilla_rotation(self, k):
        if k == 0:
            k = self.N
        theta = 2 * math.asin(self.C * self.N * self.t / (2 * math.pi * k))
        return cirq.ry(theta)


class HHLAlgorithm:
    """A class for the HHL algorithm.

    It provides two versions of the algorithm: one with amplitude amplification,
    and one without.
    """

    def __init__(
        self, A: np.ndarray, C: float, t: float, register_size: int, *input_prep_gates: cirq.Gate
    ):
        """Initializes the HHL algorithm.

        Args:
            A: The input Hermitian matrix.
            C: Algorithm parameter, see above.
            t: Algorithm parameter, see above.
            register_size: The size of the eigenvalue register.
            *input_prep_gates: A list of gates to be applied to |0> to generate the desired input
                state |b>.
        """
        self.A = A
        self.C = C
        self.t = t
        self.register_size = register_size
        self.input_prep_gates = input_prep_gates

        # ancilla qubit
        self.ancilla = cirq.LineQubit(0)
        # to store eigenvalues of the matrix
        self.register = [cirq.LineQubit(i + 1) for i in range(register_size)]
        # to store input and output vectors
        self.memory = cirq.LineQubit(register_size + 1)

    def hhl_circuit(self):
        """Constructs the HHL circuit.

        Returns:
            The HHL circuit. The ancilla measurement has key 'a' and the memory measurement
            is in key 'm'. If the ancilla is measured as 1, the memory is in the desired
            state |x>.
        """
        c = cirq.Circuit()
        hs = HamiltonianSimulation(self.A, self.t)
        pe = PhaseEstimation(self.register_size + 1, hs)
        c.append([gate(self.memory) for gate in self.input_prep_gates])
        c.append(
            [
                pe(*(self.register + [self.memory])),
                EigenRotation(self.register_size + 1, self.C, self.t)(
                    *(self.register + [self.ancilla])
                ),
                pe(*(self.register + [self.memory])) ** -1,
            ]
        )
        return c

    def diffusion_operator(self):
        """Creates the diffusion operator I - 2|0><0| which reflects about the initial state
        with all qubits in |0>.
        """
        qubits = [self.ancilla] + self.register + [self.memory]
        c = cirq.Circuit()
        c.append(cirq.X.on_each(qubits))
        c.append(cirq.ControlledGate(cirq.Z, num_controls=self.register_size + 1).on(*qubits))
        c.append(cirq.X.on_each(qubits))
        return c

    def amplitude_amplification(self, num_iterations: int):
        """Applies amplitude amplification to increase the probability of measuring the ancilla
        qubit as 1.

        Args:
            num_iterations: The number of times to apply amplitude amplification.

        Returns:
            The circuit with amplitude amplification applied.
        """
        hhl_circuit = self.hhl_circuit()
        c = cirq.Circuit()
        c += hhl_circuit

        r_succ = cirq.Z(self.ancilla)
        r_init = self.diffusion_operator()

        for _ in range(num_iterations):
            c.append(r_succ)
            c += hhl_circuit**-1
            c += r_init
            c += hhl_circuit

        return c

    def measure_circuit(self, circuit: cirq.Circuit):
        """Measures the ancilla qubit and the memory qubit.

        There are two parameters in the circuit, `exponent` and `phase_exponent` corresponding
        to a possible rotation applied before the measurement on the memory with a
        `cirq.PhasedXPowGate`.

        Args:
            circuit: The circuit to measure.

        Returns:
            The circuit with the ancilla and memory qubits measured.
        """
        circuit.append(cirq.measure(self.ancilla, key='a'))
        circuit.append(
            [
                cirq.PhasedXPowGate(
                    exponent=sympy.Symbol('exponent'), phase_exponent=sympy.Symbol('phase_exponent')
                )(self.memory),
                cirq.measure(self.memory, key='m'),
            ]
        )

        return circuit

    def print_results(self, results: list[np.ndarray], num_runs: list[int]) -> np.floating[Any]:
        """Prints the measurement results for X, Y, and Z measurements.

        Args:
            results: List containing memory qubit measurements for X, Y and Z.
            num_runs: List containing the total number of simulation runs to obtain X, Y and Z.

        Returns:
            the success probability, as the mean of (# of successful runs) / (# of runs) needed
            to obtain an estimate for X, Y and Z.
        """
        success_probability = []

        for label, result, runs in zip(('X', 'Y', 'Z'), results, num_runs):
            expectation = 1 - 2 * np.mean(result)
            num_estimates = result.shape[0]
            print(
                f'{label} = {expectation} from {num_estimates} estimates '
                f'out of {runs} simulation runs'
            )
            success_probability.append(num_estimates / runs)

        return np.mean(success_probability)

    def simulate_without_amplification(self, total_runs: int) -> list[np.ndarray]:
        """Simulates the HHL circuit without amplitude amplification.

        At the end measures X, Y and Z on the memory qubit.
        Only the runs where the ancilla is measured as 1 are returned.

        Args:
            total_runs: The total number of simulations to run.

        Returns:
            A list containing the simulation results where the ancilla is 1, for each
            X, Y and Z measurement.
        """
        simulator = cirq.Simulator()

        # Cases for measuring X, Y, and Z (respectively) on the memory qubit.
        params: list[Mapping[str, float]] = [
            {'exponent': 0.5, 'phase_exponent': -0.5},
            {'exponent': 0.5, 'phase_exponent': 0},
            {'exponent': 0, 'phase_exponent': 0},
        ]

        results = simulator.run_sweep(
            self.measure_circuit(self.hhl_circuit()), params, repetitions=total_runs
        )

        return [result.measurements['m'][result.measurements['a'] == 1] for result in results]

    def simulate_with_amplification(
        self, total_estimates: int
    ) -> tuple[list[np.ndarray], list[int]]:
        """Simulates the HHL circuit with amplitude amplification.

        Amplitude amplification is applied to the HHL circuit until the ancilla is measured as 1.
        Afterwards X, Y and Z are measured on the memory qubit.

        Args:
            total_estimates: The number of estimates to obtain for each X, Y and Z measurement.

        Returns:
            A pair of lists, containing for each X, Y and Z measurement goal:
                - a numpy array with the results for successful simulations.
                - the total number of simulation runs.
        """
        simulator = cirq.Simulator()

        # Cases for measuring X, Y, and Z (respectively) on the memory qubit.
        params: list[Mapping[str, float]] = [
            {'exponent': 0.5, 'phase_exponent': -0.5},
            {'exponent': 0.5, 'phase_exponent': 0},
            {'exponent': 0, 'phase_exponent': 0},
        ]

        results = []
        runs = []
        for param in params:
            resolver = cirq.ParamResolver(param)
            results_for_param = []
            repetitions = total_estimates
            num_runs = 0  # number of calls to simulator.run
            # We use the algorithm "Quantum Searching Without Knowing Success Probability II"
            # from Kaye et al., Section 8.4.
            # We perform 9 iterations, which suffices if the success probability p of a single run
            # without amplification is at least 1/2^10.
            for l in range(1, 10):
                M = 2**l
                # Apply amplitude amplification a random number of times between 0 and M-1.
                # For M large enough, doing this twice increases the success probability to
                # 3/4 - O(1/(M*θ)), where θ is the angle such that sin^2(θ) = p, the probability
                # of success of a single run without amplification.
                for _ in range(2):
                    j = random.randint(0, M - 1)
                    result = simulator.run(
                        self.measure_circuit(self.amplitude_amplification(num_iterations=j)),
                        resolver,
                        repetitions,
                    )
                    num_runs += repetitions
                    results_for_param.append(
                        result.measurements['m'][result.measurements['a'] == 1]
                    )
                    repetitions -= (result.measurements['a'] == 1).sum()
                    if repetitions <= 0:
                        break
                if repetitions <= 0:
                    break

            results.append(np.concatenate(results_for_param, axis=0))
            runs.append(num_runs)

        return results, runs


def main():
    """The main program loop.

    Simulates HHL with matrix input, and outputs Pauli observables of the resulting qubit state |x>.
    Expected observables are calculated from the expected solution |x>.
    """

    # Eigendecomposition:
    #   (4.537, [-0.971555, -0.0578339+0.229643j])
    #   (0.349, [-0.236813, 0.237270-0.942137j])
    # |b> = (0.64510-0.47848j, 0.35490-0.47848j)
    # |x> = (-0.0662724-0.214548j, 0.784392-0.578192j)
    A = np.array(
        [
            [4.30213466 - 6.01593490e-08j, 0.23531802 + 9.34386156e-01j],
            [0.23531882 - 9.34388383e-01j, 0.58386534 + 6.01593489e-08j],
        ]
    )
    t = 0.358166 * math.pi
    register_size = 4
    input_prep_gates = [cirq.rx(1.276359), cirq.rz(1.276359)]
    expected = (0.144130, 0.413217, -0.899154)

    # Set C to be the smallest eigenvalue that can be represented by the
    # circuit.
    C = 2 * math.pi / (2**register_size * t)

    # Simulate circuit.
    print('Expected observable outputs:')
    print('X =', expected[0])
    print('Y =', expected[1])
    print('Z =', expected[2])

    hhl = HHLAlgorithm(A, C, t, register_size, *input_prep_gates)
    print('Actual values without amplitude amplification: ')
    results = hhl.simulate_without_amplification(total_runs=5000)
    success_probability = hhl.print_results(results, num_runs=[5000, 5000, 5000])
    print(
        f'Success probability of a single run without amplitude amplification: '
        f'{success_probability}'
    )
    # We show that amplitude amplification obtains similar values using fewer simulations.
    # Because amplitude amplification continues until the algorithm succeeds, the number of
    # estimates is set to 5000 * success_probability to be roughly equivalent to 5000 runs
    # without amplitude amplification.
    print('Actual values with amplitude amplification: ')
    results, num_runs = hhl.simulate_with_amplification(
        total_estimates=int(5000 * success_probability)
    )
    success_probability = hhl.print_results(results, num_runs)
    print(
        f'Success probability of a single run with amplitude amplification: '
        f'{success_probability}'
    )


if __name__ == '__main__':
    main()
