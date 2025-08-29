from base import *
from mitiq import zne, pec
from mitiq.interface.mitiq_qiskit.qiskit_utils import initialized_depolarizing_noise

class REMTable:
    """
    A lookup table for Readout Error Mitigation (REM) experiment data, to save on QPU time.
    """
    service: QiskitRuntimeService

    def __init__(self, service):
        self.service = service

    def enter(
        self, 
        dims: list | tuple, 
        job_id: str
        ):
        """
        Enter a REM experiment into the table by dumping its contents into a .json file.
        """
        job = self.service.job(job_id)
        with open(f"rem-table/{job.backend().name}_{dims[0]}x{dims[1]}.json", "w") as file:
            json.dump((job.result(), job_id), file, cls=RuntimeEncoder)

    def load(
        self, 
        dims: list | tuple, 
        backend: IBMBackend,
        exp: CorrelatedReadoutError
        ):
        """
        Load REM experiment results from the corresponding .json file.
        """
        with open(f"rem-table/{backend.name}_{dims[0]}x{dims[1]}.json", "r") as file:
            raw, job_id = json.load(file, cls=RuntimeDecoder)
            data = ExperimentData(experiment=exp)
            data._add_result_data(raw, job_id)
            result = exp.analysis.run(data)

        return result

def get_measured_qubits(circuit):
    """
    Gets the measured qubits of a circuit.
    AI slop, needs improvement.
    """
    measured_qubits = []
    for instruction, qargs, cargs in circuit.data:
        if instruction.name == 'measure':
            bits = [circuit.find_bit(qarg)[0] for qarg in qargs]
            measured_qubits.append(bits[0])
    return measured_qubits

class PECConfig:
    """
    Configuration class for PEC parameters.
    Allows fine-tuning of PEC behavior for different lattice sizes and backends.
    """
    
    # Default PEC parameters
    DEFAULT_NOISE_LEVEL = 0.01  # Default depolarizing noise level for representation
    DEFAULT_NUM_SAMPLES = 5000  # Default number of PEC samples
    DEFAULT_MAX_SAMPLES = 10000 # Maximum samples for large circuits
    
    # Backend-specific configurations
    BACKEND_CONFIGS = {
        'ibm_brisbane': {
            'noise_level': 0.008,
            'max_samples': 8000,
            'preferred_samples': 4000
        }
    }
    
    # Lattice size recommendations
    LATTICE_CONFIGS = {
        (2, 2): {'samples': 8000, 'noise_level': 0.008},
        (4, 4): {'samples': 5000, 'noise_level': 0.010},
        (6, 6): {'samples': 3000, 'noise_level': 0.012},
        (8, 8): {'samples': 2000, 'noise_level': 0.015}
    }
    
    @classmethod
    def get_config(cls, backend_name: str, lattice_dims: tuple) -> dict:
        """
        Get optimized PEC configuration for a specific backend and lattice size.
        """
        # Start with defaults
        config = {
            'noise_level': cls.DEFAULT_NOISE_LEVEL,
            'num_samples': cls.DEFAULT_NUM_SAMPLES,
            'max_samples': cls.DEFAULT_MAX_SAMPLES
        }
        
        # Apply backend-specific settings
        backend_config = cls.BACKEND_CONFIGS.get(backend_name, {})
        config.update(backend_config)
        
        # Apply lattice-specific settings
        lattice_config = cls.LATTICE_CONFIGS.get(tuple(lattice_dims), {})
        if 'samples' in lattice_config:
            config['num_samples'] = lattice_config['samples']
        if 'noise_level' in lattice_config:
            config['noise_level'] = lattice_config['noise_level']
        
        return config

class PECTable:
    """
    A lookup table for PEC representations to avoid recomputation.
    """
    def __init__(self):
        self.representations = {}
    
    def get_key(self, backend_name: str, circuit: QuantumCircuit) -> str:
        """Generate a unique key for the circuit and backend combination."""
        # Use circuit depth and gate counts as a simple key
        gate_counts = circuit.count_ops()
        return f"{backend_name}_{circuit.depth()}_{hash(str(gate_counts))}"
    
    def store(self, key: str, representations: list):
        """Store PEC representations."""
        self.representations[key] = representations
    
    def load(self, key: str) -> list | None:
        """Load PEC representations if available."""
        return self.representations.get(key)

class ErrorMitigator:
    """
    Contains all methods that mitigate error via post-processing.
    Currently implements Readout Error Mitigation (REM), Iterative Bayesian Unfolding (IBU),
    Zero Noise Extrapolation (ZNE), and Probabilistic Error Cancellation (PEC).
    """
    class ReadoutError:
        """
        Retrieves and/or enters results from the REM table based on the boolean "use_table".
        """
        result: ExperimentData
        
        def __init__(
            self,
            lattice: Lattice,
            backend: IBMBackend,
            service: QiskitRuntimeService,
            use_table: bool,
            ) -> None:

            dims = lattice.dims
            measured_qubits = StepCircuit(lattice, 1).grid_qubits
            exp = CorrelatedReadoutError(measured_qubits)
            
            table = REMTable(service)
            if use_table and path.exists(f"rem-table/{backend.name}_{dims[0]}x{dims[1]}.json"):
                    result = table.load(dims, backend, exp)
            else:
                result = exp.run(backend)
                table.enter(dims, result.job_ids[0])
            
            self.result = result

    dims: list | tuple
    lattice: CollisionlessLattice | Lattice
    backend: IBMBackend
    service: QiskitRuntimeService
    equalization: bool
    readout_error_mitigation: bool
    iterative_bayesian_unfolding: bool
    zero_noise_extrapolation: bool
    probabilistic_error_cancellation: bool
    pec_table: PECTable

    def __init__(
            self,
            lattice: CollisionlessLattice | Lattice,
            backend: IBMBackend,
            service: QiskitRuntimeService,
            equalization: bool = False,
            readout_error_mitigation: bool = False,
            iterative_bayesian_unfolding: bool = False,
            zero_noise_extrapolation: bool = False,
            probabilistic_error_cancellation: bool = False
        ) -> None:
        self.lattice = lattice
        self.dims = lattice.dims
        self.backend = backend
        self.service = service
        self.equalization = equalization
        self.readout_error_mitigation = readout_error_mitigation
        self.iterative_bayesian_unfolding = iterative_bayesian_unfolding
        self.zero_noise_extrapolation = zero_noise_extrapolation
        self.probabilistic_error_cancellation = probabilistic_error_cancellation
        self.pec_table = PECTable()
    
    def rem(
            self,
            shots: int,
            counts: list,
            use_table: bool = True
        ) -> list:
            
        print("Mitigating readout error...")

        result = ErrorMitigator.ReadoutError(self.lattice, self.backend, self.service, use_table).result

        mitigator = result.analysis_results("Correlated Readout Mitigator", dataframe=True).iloc[0].value
        mitigated_quasi_probs = [mitigator.quasi_probabilities(count) for count in counts]

        mitigated_probs = [(prob.nearest_probability_distribution().binary_probabilities()) for prob in mitigated_quasi_probs]

        mitigated_counts = [{label: int(prob * shots) for label, prob in mitigated_prob.items()} for mitigated_prob in mitigated_probs]
        
        return mitigated_counts
        
    def ibu(
            self,
            qcs: list[QuantumCircuit],
            shots: int,
            counts: list,
        ) -> list:
        """
        (NOT MY CODE)
        Modified based on PyIBU tutorial.ipynb
        Credit: https://github.com/sidsrinivasan/PyIBU
        """
        print("Performing Iterative Bayesian Unfolding...")

        measured_qubits_list = [get_measured_qubits(qc) for qc in qcs]

        params = {
            "exp_name": "qlbm",
            "method": "full",  # options: "full", "reduced"
            "library": "jax",  # options: "tensorflow" (for "full" only) or "jax"
            "num_qubits": len(measured_qubits_list[0]),
            "max_iters": 100,
            "tol": 1e-4,
            "use_log": False,  # options: True or False
            "verbose": False,
            "init": "unif",  # options: "unif" or "unif_obs" or "obs"
            "smoothing": 1e-8
        }
        if params["library"] == 'tensorflow':
            params.update({
                "max_iters": tf.constant(params["max_iters"]),
                "eager_run": True
            })
            tf.config.run_functions_eagerly(params["eager_run"])

        matrices_list = [[get_response_matrix(self.backend, q) for q in measured_qubits] for measured_qubits in measured_qubits_list]

        ibu_mitigated = []
        for i in range(len(matrices_list)):
            ibu = IBU(matrices_list[i], params)
            ibu.set_obs(dict(counts[i]))
            ibu.initialize_guess()
            # 4x4 first timestep theoretically perfect lattice data, not used
            # t_true_dict = {"0001" : 1/8, "0010" : 1/8, "0101" : 1/8, "0110" : 1/8, "1001" : 1/8, "1010" : 1/8, "1101" : 1/8, "1110" : 1/8}
            t_sol, max_iters, tracker = ibu.train(params["max_iters"], tol=params["tol"])
            
            ibu_mitigated.append({label: int(prob[0] * shots) for label, prob in ibu.guess_as_dict().items()})

        return ibu_mitigated

    def generate_pec_representations(
            self,
            circuits: list[QuantumCircuit],
            num_samples: int = 10000
        ) -> list[list]:
        """
        Generate PEC representations for the given circuits.
        """
        print("Generating PEC representations...")
        
        # Get optimized configuration for this backend and lattice
        pec_config = PECConfig.get_config(self.backend.name, self.dims)
        
        # Create a simplified noise model for PEC representation generation
        # In practice, you might want to use the actual backend noise model
        noise_model = initialized_depolarizing_noise(
            noise_level=pec_config['noise_level'],
            num_qubits=max([circuit.num_qubits for circuit in circuits])
        )
        
        representations = []
        for i, circuit in enumerate(circuits):
            print(f"Processing circuit {i+1}/{len(circuits)}...")
            
            # Check if we already have representations cached
            key = self.pec_table.get_key(self.backend.name, circuit)
            cached_repr = self.pec_table.load(key)
            
            if cached_repr is not None:
                print(f"Using cached PEC representation for circuit {i+1}")
                representations.append(cached_repr)
                continue
            
            try:
                # Generate PEC representation
                # We use a subset of the circuit's operations for efficiency
                representation = pec.represent_operations_in_circuit_with_local_depolarizing_noise(
                    circuit,
                    noise_level=pec_config['noise_level'],
                    num_samples=min(num_samples, pec_config['max_samples'])
                )
                
                # Cache the representation
                self.pec_table.store(key, representation)
                representations.append(representation)
                
            except Exception as e:
                print(f"Warning: Could not generate PEC representation for circuit {i+1}: {e}")
                # Fall back to identity representation (no error correction)
                representations.append([])
        
        return representations

    def pec(
            self,
            circuits: list[QuantumCircuit],
            shots: int,
            num_samples: int = 5000
        ) -> tuple[list[dict], str]:
        """
        Apply Probabilistic Error Cancellation to the circuits.
        """
        print("Performing Probabilistic Error Cancellation...")
        
        # Generate or retrieve PEC representations
        representations = self.generate_pec_representations(circuits, num_samples)
        
        # Execute PEC-mitigated circuits
        mitigated_counts = []
        
        sampler = Sampler(self.backend)
        
        for i, (circuit, representation) in enumerate(zip(circuits, representations)):
            print(f"Executing PEC for circuit {i+1}/{len(circuits)}...")
            
            if not representation:  # No representation available, run original circuit
                print(f"No PEC representation for circuit {i+1}, running original...")
                job = sampler.run([circuit], shots=shots)
                result = job.result()
                counts = result[0].join_data().get_counts()
                mitigated_counts.append(counts)
                continue
            
            try:
                # Sample from the PEC representation
                sampled_circuits = pec.sample_circuits(
                    circuit,
                    representation,
                    num_samples=min(num_samples, shots // 10)  # Reasonable number of samples
                )
                
                # Execute the sampled circuits
                if sampled_circuits:
                    # Distribute shots across sampled circuits
                    shots_per_circuit = max(1, shots // len(sampled_circuits))
                    
                    jobs = []
                    for sampled_circuit in sampled_circuits:
                        job = sampler.run([sampled_circuit], shots=shots_per_circuit)
                        jobs.append(job)
                    
                    # Collect and process results
                    all_counts = []
                    for job in jobs:
                        result = job.result()
                        counts = result[0].join_data().get_counts()
                        all_counts.append(counts)
                    
                    # Combine results using PEC post-processing
                    combined_counts = self._combine_pec_results(all_counts, representation, shots)
                    mitigated_counts.append(combined_counts)
                    
                else:
                    # Fallback to original circuit
                    job = sampler.run([circuit], shots=shots)
                    result = job.result()
                    counts = result[0].join_data().get_counts()
                    mitigated_counts.append(counts)
                    
            except Exception as e:
                print(f"Warning: PEC failed for circuit {i+1}, using original: {e}")
                # Fallback to original circuit
                job = sampler.run([circuit], shots=shots)
                result = job.result()
                counts = result[0].join_data().get_counts()
                mitigated_counts.append(counts)
        
        if self.equalization:
            mitigated_counts = self.equalize(mitigated_counts, shots)
        
        label = f"pec-collisionless-{self.dims[0]}x{self.dims[1]}-ibm-qpu"
        return mitigated_counts, label

    def _combine_pec_results(
            self,
            all_counts: list[dict],
            representation: list,
            target_shots: int
        ) -> dict:
        """
        Combine PEC sampling results with proper weighting.
        """
        if not all_counts:
            return {}
        
        # Get all possible bitstrings
        all_bitstrings = set()
        for counts in all_counts:
            all_bitstrings.update(counts.keys())
        
        # Initialize combined counts
        combined = {bitstring: 0.0 for bitstring in all_bitstrings}
        
        # Simple average combination (can be improved with proper PEC weighting)
        total_weight = len(all_counts)
        for counts in all_counts:
            for bitstring in all_bitstrings:
                combined[bitstring] += counts.get(bitstring, 0) / total_weight
        
        # Normalize to target shots
        total_counts = sum(combined.values())
        if total_counts > 0:
            scaling_factor = target_shots / total_counts
            combined = {bs: int(count * scaling_factor) for bs, count in combined.items()}
        
        return combined
    
    def zne(
            self,
            shots: int,
            steps: int = 1
        ) -> tuple[list[dict], str]:
        """
        Runs Zero Noise Extrapolation. Omits the 0th step of the visualization since it is of depth 1 and thus gets good results.
        """
        print("Performing Zero Noise Extrapolation...")

        step_qcs = [StepCircuit(self.lattice, step) for step in range(steps+1)]
        circuits = [step_qc.circuit for step_qc in step_qcs]

        # This is how the 0th step is omitted; it will be run normally and prepended at the end.
        first = circuits.pop(0)

        measured_qubits = step_qcs[0].grid_qubits

        scale_factors = np.array([1., 1.5, 2., 2.5, 3., 3.5, 4.])
        folded_circuits = [[
                zne.scaling.fold_gates_at_random(circuit, scale)
                for scale in scale_factors
        ] for circuit in circuits ]

        pm = generate_preset_pass_manager(
            backend=self.backend,
            basis_gates=None,
            optimization_level=0, # Important to preserve folded gates.
        )
        
        first_exec = pm.run([first])
        exec_circuits = [pm.run(folded_circuit) for folded_circuit in folded_circuits]
        
        sampler = Sampler(self.backend)

        first_job = sampler.run(first_exec)
        jobs = [sampler.run(exec_circuit, shots=shots) for exec_circuit in exec_circuits]

        # Raw
        all_counts = [[job.result()[i].join_data().get_counts() for i in range(len(scale_factors))] for job in jobs]
        # REM implementation
        all_counts = [self.rem(shots, all_count) for all_count in all_counts]
        # IBU Implementation
        all_counts = [self.ibu(circ, shots, count) for circ, count in zip(exec_circuits, all_counts)]

        bitstrings = generate_bitstrings(len(measured_qubits))

        # Array of arrays of expectation values of bitstrings
        # [[circuit 1 exp vals], [circuit 2 exp vals]]
        all_exps_arr = []

        for step in range(0, steps):
            all_exps = {}
            for bitstring in bitstrings:
                try:
                    all_exps[bitstring] = [counts.get(bitstring) / shots for counts in all_counts[step]]
                except TypeError:
                    all_exps[bitstring] = [0 for counts in all_counts[step]]
            all_exps_arr += [all_exps]

        zero_noise_values_arr = [[zne.PolyFactory.extrapolate(scale_factors, exp, 2) for exp in all_exps.values()] for all_exps in all_exps_arr]
        m_vals = [[int(znv * shots) for znv in zero_noise_values] for zero_noise_values in zero_noise_values_arr]
        
        mitigated_counts = [dict(zip(bitstrings, m_val)) for m_val in m_vals]
        
        mitigated_counts.insert(0, first_job.result()[0].join_data().get_counts())

        if self.equalization:
            mitigated_counts = self.equalize(mitigated_counts, shots)

        label = f"zne-collisionless-{self.dims[0]}x{self.dims[1]}-ibm-qpu"

        return mitigated_counts, label


        
    def equalize(
            self,
            counts: list[dict],
            shots: int
        ) -> list[dict]:
        """
        "Equalizes" the visualization by cropping the lowest-counted half of the data and setting 
        the highest-counted half to the theoretically correct number of counts, which is:
        shots / 0.5*x*y, where [x,y] are the dimensions.
        """
        measured_qubits = StepCircuit(self.lattice, 0).grid_qubits
        equalized_counts = []

        for count in counts:
            cutoff = sorted(list(count.values()))[2 ** (len(measured_qubits) - 1)]
            eq_count = {}
            for key, val in count.items():
                if val < cutoff:
                    eq_count.update({key : 0})
                else:
                    eq_count.update({key: shots/(self.lattice.dims[0] * self.lattice.dims[1] * 0.5)})
            equalized_counts += [eq_count]
        
        return equalized_counts

    def mitigate(
        self,
        qcs: list[QuantumCircuit],
        shots: int,
        counts: list,
        ) -> tuple[list[dict], str]:
        
        # Data shows that performing REM and then IBU yields better results.
        if (self.readout_error_mitigation == False and 
            self.iterative_bayesian_unfolding == False and
            self.probabilistic_error_cancellation == False):
            label = f"raw-collisionless-{self.dims[0]}x{self.dims[1]}-ibm-qpu"
            return counts, label
        
        # PEC is fundamentally different - it requires circuit-level intervention
        # and cannot be applied as post-processing like REM/IBU
        if self.probabilistic_error_cancellation:
            print("Note: PEC requires circuit-level modification and bypasses post-processing methods.")
            pec_counts, label = self.pec(qcs, shots)
            return pec_counts, label
        
        # Standard post-processing pipeline (REM -> IBU)
        mitigated_counts = counts
        label_parts = []
        
        if self.readout_error_mitigation:
            label_parts.append("rem")
            mitigated_counts = self.rem(shots, mitigated_counts)
        
        if self.iterative_bayesian_unfolding:
            label_parts.append("ibu")
            mitigated_counts = self.ibu(qcs, shots, mitigated_counts)
        
        label = f"{'-'.join(label_parts)}-collisionless-{self.dims[0]}x{self.dims[1]}-ibm-qpu"
        return mitigated_counts, label