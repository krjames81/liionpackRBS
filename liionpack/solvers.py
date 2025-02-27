#
# Solvers
#
import liionpack as lp
#KRJ - Got rid of the import of cco from solver_utils
#from liionpack.solver_utils import _create_casadi_objects as cco
from liionpack.solver_utils import _serial_step as ss
from liionpack.solver_utils import _mapped_step as ms
from liionpack.solver_utils import _serial_eval as se
from liionpack.solver_utils import _mapped_eval as me
import ray
import numpy as np
import time as ticker
from tqdm import tqdm
import pybamm
import casadi #KRJ - Need to import casadi here so cco can use it

#KRJ - Added my_cco here
def my_cco(inputs, sim, dt, Nspm, nproc, variable_names, mapped, simlist):
    """
    Internal function to produce the casadi objects in their mapped form for
    parallel evaluation

    Args:
        inputs (dict):
            initial guess for inputs (not used for simulation).
        sim (pybamm.Simulation):
            A PyBaMM simulation object that contains the model, parameter values,
            solver, solution etc.
        dt (float):
            The time interval (in seconds) for a single timestep. Fixed throughout
            the simulation
        Nspm (int):
            Number of individual batteries in the pack.
        nproc (int):
            Number of parallel processes to map to.
        variable_names (list):
            Variables to evaluate during solve. Must be a valid key in the
            model.variables
        mapped (bool):
            Use the mapped casadi objects, default is True

    Returns:
        integrator (mapped casadi.integrator):
            Solves an initial value problem (IVP) coupled to a terminal value
            problem with differential equation given as an implicit ODE coupled
            to an algebraic equation and a set of quadratures
        variables_fn (mapped variables evaluator):
            evaluates the simulation and output variables. see casadi function
        t_eval (np.ndarray):
            Float array of times to evaluate.
            times to evaluate in a single step, starting at zero for each step
        events_fn (mapped events evaluator):
            evaluates the event variables. see casadi function

    """
    solver = sim.solver
    # Initial solution - this builds the model behind the scenes
    sim.build()
    initial_solutions = []
    init_sol = sim.step(
        dt=1e-6, save=False, starting_solution=None, inputs=inputs[0]
    ).last_state
    # evaluate initial condition
    model = sim.built_model
    y0_total_size = (
        model.len_rhs + model.len_rhs_sens + model.len_alg + model.len_alg_sens
    )
    y_zero = np.zeros((y0_total_size, 1))
    for inpt in inputs:
        inputs_casadi = casadi.vertcat(*[x for x in inpt.values()])
        initial_solutions.append(init_sol.copy())
        _init = model.initial_conditions_eval(0, y_zero, inputs_casadi)
        initial_solutions[-1].y[:] = _init

    # Step model forward dt seconds
    t_eval = np.linspace(0, dt, 11)

    # No external variables - Temperature solved as lumped model in pybamm
    # External variables could (and should) be used if battery thermal problem
    # Includes conduction with any other circuits or neighboring batteries
    # inp_and_ext.update(external_variables)
    inp_and_ext = inputs

    # Code to create mapped integrator
    integrator = solver.create_integrator(
        sim.built_model, inputs=inp_and_ext, t_eval=t_eval
    )
    if mapped:
        integrator = integrator.map(Nspm, "thread", nproc)
    # Get the input parameter order
    ip_order = inputs[0].keys()
    # Variables function for parallel evaluation
    casadi_objs = sim.built_model.export_casadi_objects(
        variable_names=variable_names, input_parameter_order=ip_order
    )
    variables = casadi_objs["variables"]
    t, x, z, p = (
        casadi_objs["t"],
        casadi_objs["x"],
        casadi_objs["z"],
        casadi_objs["inputs"],
    )
    variables_stacked = casadi.vertcat(*variables.values())
    variables_fn = casadi.Function("variables", [t, x, z, p], [variables_stacked])
    if mapped:
        variables_fn = variables_fn.map(Nspm, "thread", nproc)

    # Look for events in model variables and create a function to evaluate them
    all_vars = sorted(sim.model.variables.keys())
    event_vars = [v for v in all_vars if "Event" in v]
    if len(event_vars) > 0:
        # Variables function for parallel evaluation
        casadi_objs = sim.built_model.export_casadi_objects(
            variable_names=event_vars, input_parameter_order=ip_order
        )
        events = casadi_objs["variables"]
        t, x, z, p = (
            casadi_objs["t"],
            casadi_objs["x"],
            casadi_objs["z"],
            casadi_objs["inputs"],
        )
        events_stacked = casadi.vertcat(*events.values())
        events_fn = casadi.Function("variables", [t, x, z, p], [events_stacked])
        if mapped:
            events_fn = events_fn.map(Nspm, "thread", nproc)
    else:
        events_fn = None

    output = {
        "integrator": integrator,
        "variables_fn": variables_fn,
        "t_eval": t_eval,
        "event_names": event_vars,
        "events_fn": events_fn,
        "initial_solutions": initial_solutions,
    }
    return output



class GenericActor:
    def __init__(self):
        pass

    def setup(
        self,
        Nspm,
        sim_func,
        parameter_values,
        dt,
        inputs,
        variable_names,
        initial_soc,
        nproc,
        simlist,
    ):
        print("Setup has started")
        # Casadi specific arguments
        if nproc > 1:
            mapped = True
        else:
            mapped = False
        self.Nspm = Nspm
        # Set up simulation
        self.parameter_values = parameter_values
        if initial_soc is not None:
            if (
                (type(initial_soc) in [float, int])
                or (isinstance(initial_soc, list) and len(initial_soc) == 1)
                or (isinstance(initial_soc, np.ndarray) and len(initial_soc) == 1)
            ):
                _, _ = lp.update_init_conc(parameter_values, initial_soc, update=True)
            else:
                lp.logger.warning(
                    "Using a list or an array of initial_soc "
                    + "is not supported, please set the initial "
                    + "concentrations via inputs"
                )
        if sim_func is None:
            self.simulation = lp.basic_simulation(self.parameter_values)
        else:
            self.simulation = sim_func(self.parameter_values)

        # Set up integrator
        #KRJ added "lp." in front of my_cco so it points to the
        #editable lp.my_cco, and not the local function defined above.
        casadi_objs = lp.my_cco(
            inputs, self.simulation, dt, Nspm, nproc, variable_names, mapped, simlist
        )
        self.model = self.simulation.built_model
        self.integrator = casadi_objs["integrator"]
        self.variables_fn = casadi_objs["variables_fn"]
        self.t_eval = casadi_objs["t_eval"]
        self.event_names = casadi_objs["event_names"]
        self.events_fn = casadi_objs["events_fn"]
        self.step_solutions = casadi_objs["initial_solutions"]
        self.last_events = None
        self.event_change = None
        if mapped:
            self.step_fn = ms
            self.eval_fn = me
        else:
            self.step_fn = ss
            self.eval_fn = se

    def step(self, inputs):
        # Solver Step
        self.step_solutions, self.var_eval, self.events_eval = self.step_fn(
            self.simulation.built_model,
            self.step_solutions,
            inputs,
            self.integrator,
            self.variables_fn,
            self.t_eval,
            self.events_fn,
        )
        return self.check_events()

    def evaluate(self, inputs):
        self.var_eval = self.eval_fn(
            self.simulation.built_model,
            self.step_solutions,
            inputs,
            self.variables_fn,
            self.t_eval,
        )
        lp.logger.notice("Evaluate function running")

    def check_events(self):
        if self.last_events is not None:
            # Compare changes
            new_sign = np.sign(self.events_eval)
            old_sign = np.sign(self.last_events)
            self.event_change = (old_sign * new_sign) < 0
            self.last_events = self.events_eval
            return np.any(self.event_change)
        else:
            self.last_events = self.events_eval
            return False

    def get_event_change(self):
        return self.event_change

    def get_event_names(self):
        return self.event_names

    def output(self):
        return self.var_eval


@ray.remote(num_cpus=1)
class RayActor(GenericActor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class GenericManager:
    def __init__(
        self,
    ):
        pass

    def solve(
        self,
        netlist,
        sim_func,
        parameter_values,
        experiment,
        inputs,
        output_variables,
        initial_soc,
        nproc,
        simlist,
        node_termination_func=None,
        setup_only=False,
    ):
        self.netlist = netlist
        self.sim_func = sim_func
        self.node_termination_func = node_termination_func
        self.parameter_values = parameter_values
        self.check_current_function()
        # Get netlist indices for resistors, voltage sources, current sources
        self.Ri_map = netlist["desc"].str.find("Ri") > -1
        self.V_map = netlist["desc"].str.find("V") > -1
        self.I_map = netlist["desc"].str.find("I") > -1
        self.Terminal_Node = np.array(netlist[self.I_map].node1)
        self.Nspm = np.sum(self.V_map)

        self.split_models(self.Nspm, nproc)

        # Generate the protocol from the supplied experiment
        self.protocol_steps, self.terminations, self.step_types = (
            lp.generate_protocol_from_experiment(experiment)
        )
        self.flattened_protocol = [
            item for sublist in self.protocol_steps for item in sublist
        ]
        self.dt = experiment.period
        self.Nsteps = len(self.flattened_protocol)
        # If the step is starting with a rest the current will be zero and
        # this messes up the internal resistance calc. Add a very small current
        # for init.
        first_value = self.protocol_steps[0][0]
        if first_value == 0.0:
            netlist.loc[self.I_map, ("value")] = 1e-6
        else:
            netlist.loc[self.I_map, ("value")] = first_value
        # Solve the circuit to initialise the electrochemical models
        if self.step_types[0] == "power":
            current = None
            power = first_value
        else:
            current = first_value
            power = None

        V_node, I_batt, terminal_current, terminal_voltage, terminal_power = (
            lp.solve_circuit(netlist, current=current, power=power)
        )

        # The simulation output variables calculated at each step for each battery
        # Must be a 0D variable i.e. battery wide volume average - or X-averaged for
        # 1D model
        self.variable_names = [
            "Terminal voltage [V]",
            "Surface open-circuit voltage [V]",
        ]
        if output_variables is not None:
            for out in output_variables:
                if out not in self.variable_names:
                    self.variable_names.append(out)
            # variable_names = variable_names + output_variables
        self.Nvar = len(self.variable_names)

        # Storage variables for simulation data
        self.shm_i_app = np.zeros([self.Nsteps, self.Nspm], dtype=np.float32)
        self.shm_Ri = np.zeros([self.Nsteps, self.Nspm], dtype=np.float32)
        self.output = np.zeros([self.Nvar, self.Nsteps, self.Nspm], dtype=np.float32)
        self.node_voltages = np.zeros([self.Nsteps, len(V_node)], dtype=np.float32)

        # Initialize currents in battery models
        self.shm_i_app[0, :] = I_batt * -1

        # Initialize the node voltages
        self.node_voltages[0, :] = V_node

        # Step forward in time
        self.V_terminal = np.zeros(self.Nsteps, dtype=np.float32)
        self.I_terminal = np.zeros(self.Nsteps, dtype=np.float32)
        self.P_terminal = np.zeros(self.Nsteps, dtype=np.float32)
        self.record_times = np.zeros(self.Nsteps, dtype=np.float32)

        self.v_cut_lower = parameter_values["Lower voltage cut-off [V]"]
        self.v_cut_higher = parameter_values["Upper voltage cut-off [V]"]

        # Handle the inputs
        self.inputs = inputs
        self.inputs_dict = lp.build_inputs_dict(self.shm_i_app[0, :], self.inputs, None)
        # Solver specific setup
        self.setup_actors(nproc, self.inputs_dict, initial_soc, simlist)
        # Get the initial state of the system
        self.evaluate_actors()
        if not setup_only:
            self.global_step = 0
            for ps, step_protocol in enumerate(self.protocol_steps):
                step_termination = self.terminations[ps]
                step_type = self.step_types[ps]
                if step_termination == []:
                    step_termination = 0.0
                self._step_solve_step(step_protocol, step_termination, step_type, None)
            return self.step_output()

    def _step_solve_step(self, protocol, termination, step_type, updated_inputs):
        tic = ticker.time()

        # Do stepping
        lp.logger.notice("Starting step solve")
        vlims_ok = True
        skip_vcheck = True
        with tqdm(total=len(protocol), desc="Stepping simulation") as pbar:
            step = 0
            while step < len(protocol):
                vlims_ok = self._step(
                    step, protocol, termination, step_type, updated_inputs, skip_vcheck
                )
                skip_vcheck = False
                if vlims_ok:
                    # all good - keep going
                    self.global_step += 1
                    step += 1
                    pbar.update(1)
                else:
                    # Move on to next protocol step
                    break
        self.step = step
        toc = ticker.time()
        lp.logger.notice("Step solve finished")
        lp.logger.notice("Total stepping time " + str(np.around(toc - tic, 3)) + "s")
        lp.logger.notice(
            "Time per step " + str(np.around((toc - tic) / len(protocol), 3)) + "s"
        )

    def step_output(self):
        self.cleanup()
        self.shm_Ri = np.abs(self.shm_Ri)
        # Collect outputs
        report_steps = min(len(self.flattened_protocol), self.global_step)
        self.all_output = {}
        self.all_output["Time [s]"] = self.record_times[:report_steps]
        self.all_output["Pack current [A]"] = self.I_terminal[:report_steps]
        self.all_output["Pack terminal voltage [V]"] = self.V_terminal[:report_steps]
        self.all_output["Pack power [W]"] = self.P_terminal[:report_steps]
        self.all_output["Cell current [A]"] = self.shm_i_app[:report_steps, :]
        self.all_output["Node voltage [V]"] = self.node_voltages[:report_steps, :]
        self.all_output["Cell internal resistance [Ohm]"] = self.shm_Ri[
            :report_steps, :
        ]
        for j in range(self.Nvar):
            self.all_output[self.variable_names[j]] = self.output[j, :report_steps, :]
        return self.all_output

    def _pack_voltage(self, step):
        current_nodes = self.netlist.loc[
            self.I_map, (["node2", "node1"])
        ].values.flatten()
        return np.diff(self.node_voltages[step, current_nodes])[0]

    def _step(
        self, step, protocol, termination, step_type, updated_inputs, skip_vcheck
    ):
        vlims_ok = True
        # 01 Calculate whether resting or restarting
        self.resting = step > 0 and protocol[step] == 0.0 and protocol[step - 1] == 0.0
        self.restarting = (
            step > 0 and protocol[step] != 0.0 and protocol[step - 1] == 0.0
        )
        # 02 Get the actor output - Battery state info
        self.get_actor_output(self.global_step)
        # 03 Get the ocv and internal resistance
        temp_v = self.output[0, self.global_step, :]
        temp_ocv = self.output[1, self.global_step, :]
        # When resting and rebalancing currents are small the internal
        # resistance calculation can diverge as it's R = V / I
        # At rest the internal resistance should not change greatly
        # so for now just don't recalculate it.
        if not self.resting and not self.restarting:
            self.temp_Ri = self.calculate_internal_resistance(self.global_step)
        self.shm_Ri[self.global_step, :] = self.temp_Ri
        # 04 Update netlist
        self.netlist.loc[self.V_map, ("value")] = temp_ocv
        self.netlist.loc[self.Ri_map, ("value")] = self.temp_Ri

        # 05 Solve the circuit with updated netlist
        if step <= self.Nsteps:
            if step_type == "power":
                power = protocol[step]
                current = None
            else:
                current = protocol[step]
                power = None
            V_node, I_batt, terminal_current, terminal_voltage, terminal_power = (
                lp.solve_circuit(self.netlist, current=current, power=power)
            )
            lp.power_loss(self.netlist)
            self.record_times[self.global_step] = self.global_step * self.dt
            self.netlist.loc[self.I_map, ("value")] = terminal_current
            self.I_terminal[self.global_step] = terminal_current[0]
            self.V_terminal[self.global_step] = terminal_voltage[0]
            self.P_terminal[self.global_step] = terminal_power[0]
        if self.global_step < self.Nsteps - 1:
            # igore last step save the new currents and build inputs
            # for the next step
            I_app = I_batt[:] * -1
            self.shm_i_app[self.global_step, :] = I_app
            self.shm_i_app[self.global_step + 1, :] = I_app
            self.node_voltages[self.global_step, :] = V_node
            self.inputs_dict = lp.build_inputs_dict(I_app, self.inputs, updated_inputs)
        # 06 Check if voltage limits are reached and terminate
        if np.any(temp_v < self.v_cut_lower):
            lp.logger.warning("Low voltage limit reached")
            vlims_ok = False
        if np.any(temp_v > self.v_cut_higher):
            lp.logger.warning("High voltage limit reached")
            vlims_ok = False
        v_thresh = temp_v - termination
        if np.any(v_thresh < 0) and np.any(v_thresh > 0):
            # some have crossed the stopping condition
            vlims_ok = False
        if self.node_termination_func is not None:
            if self.node_termination_func(V_node):
                lp.logger.warning("Node voltage limit reached")
                vlims_ok = False
        if skip_vcheck:
            vlims_ok = True
        # 07 Step the electrochemical system
        self.step_actors()
        return vlims_ok

    def check_current_function(self):
        i_func = self.parameter_values["Current function [A]"]
        if i_func.__class__ is not pybamm.InputParameter:
            self.parameter_values.update({"Current function [A]": "[input]"})
            lp.logger.notice(
                "Parameter: Current function [A] has been set to " + "input"
            )

    def actor_i_app(self, index):
        actor_indices = self.split_index[index]
        return self.shm_i_app[self.timestep, actor_indices]

    def actor_htc(self, index):
        return self.htc[index]

    def build_inputs(self):
        inputs = []
        #print("Number of actors at build_inputs:",len(self.actors))
        for i in range(len(self.actors)):
            inputs.append(self.inputs_dict[self.slices[i]])
        return inputs

    def calculate_internal_resistance(self, step):
        # Calculate internal resistance and update netlist
        temp_v = self.output[0, step, :]
        temp_ocv = self.output[1, step, :]
        temp_I = self.shm_i_app[step, :]
        temp_Ri = np.abs((temp_ocv - temp_v) / temp_I)
        temp_Ri[temp_Ri == 0.0] = 1e-6
        return temp_Ri

    def update_external_variables(self):
        # This is probably going to involve reading from disc unless the whole
        # algorithm is wrapped inside an "external" solver
        # For now use a dummy function to test changing the values
        pass

    def split_models(self, Nspm, nproc):
        pass

    def setup_actors(self, nproc, inputs, initial_soc, simlist):
        pass

    def step_actors(self):
        pass

    def evaluate_actors(self):
        pass

    def get_actor_output(self, step):
        pass

    def cleanup(self):
        pass


class RayManager(GenericManager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        lp.logger.notice("Ray initialization started")
        ray.init()
        lp.logger.notice("Ray initialization complete")

    def split_models(self, Nspm, nproc):
        # Manage the number of SPM models per worker
        self.split_index = np.array_split(np.arange(Nspm), nproc)
        self.spm_per_worker = [len(s) for s in self.split_index]
        self.slices = []
        for i in range(nproc):
            self.slices.append(
                slice(self.split_index[i][0], self.split_index[i][-1] + 1)
            )

    def setup_actors(self, nproc, inputs, initial_soc, simlist):
        tic = ticker.time()
        # Ray setup an actor for each worker
        self.actors = []
        for i in range(nproc):
            self.actors.append(lp.RayActor.remote())
        setup_futures = []
        for i, a in enumerate(self.actors):
            # Create actor on each worker containing a simulation
            setup_futures.append(
                a.setup.remote(
                    Nspm=self.spm_per_worker[i],
                    sim_func=self.sim_func,
                    parameter_values=self.parameter_values,
                    dt=self.dt,
                    inputs=inputs[self.slices[i]],
                    variable_names=self.variable_names,
                    initial_soc=initial_soc,
                    nproc=1,
                    simlist=simlist,
                )
            )
        _ = [ray.get(f) for f in setup_futures]
        toc = ticker.time()
        lp.logger.notice(
            "Ray actors setup in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def step_actors(self):
        t1 = ticker.time()
        future_steps = []
        inputs = self.build_inputs()
        for i, pa in enumerate(self.actors):
            future_steps.append(pa.step.remote(inputs[i]))
        events = [ray.get(fs) for fs in future_steps]
        if np.any(events):
            self.log_event()
        t2 = ticker.time()
        lp.logger.info("Ray actors stepped in " + str(np.around(t2 - t1, 3)) + "s")

    def evaluate_actors(self):
        t1 = ticker.time()
        future_evals = []
        inputs = self.build_inputs()
        for i, pa in enumerate(self.actors):
            future_evals.append(pa.evaluate.remote(inputs[i]))
        _ = [ray.get(fs) for fs in future_evals]
        t2 = ticker.time()
        lp.logger.info("Ray actors evaluated in " + str(np.around(t2 - t1, 3)) + "s")

    def get_actor_output(self, step):
        t1 = ticker.time()
        futures = []
        for actor in self.actors:
            futures.append(actor.output.remote())
        for i, f in enumerate(futures):
            out = ray.get(f)
            self.output[:, step, self.split_index[i]] = out
        t2 = ticker.time()
        lp.logger.info(
            "Ray actor output retrieved in " + str(np.around(t2 - t1, 3)) + "s"
        )

    def log_event(self):
        futures = []
        for actor in self.actors:
            futures.append(actor.get_event_change.remote())
        all_event_changes = []
        for i, f in enumerate(futures):
            all_event_changes.append(np.asarray(ray.get(f)))
        event_change = np.hstack(all_event_changes)
        Nr, Nc = event_change.shape
        event_names = ray.get(self.actors[0].get_event_names.remote())
        for r in range(Nr):
            if np.any(event_change[r, :]):
                lp.logger.warning(
                    event_names[r]
                    + ", Batteries: "
                    + str(np.where(event_change[r, :])[0].tolist())
                )

    def cleanup(self):
        for actor in self.actors:
            ray.kill(actor)
        lp.logger.notice("Shutting down Ray")
        ray.shutdown()


class CasadiManager(GenericManager):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def split_models(self, Nspm, nproc):
        # For casadi there is no need to split the models as we pass them all
        # to the integrator however we still want the global variables to be
        # used in the same generic way
        self.spm_per_worker = Nspm
        self.split_index = np.array_split(np.arange(Nspm), 1)
        self.slices = [slice(self.split_index[0][0], self.split_index[0][-1] + 1)]

    def setup_actors(self, nproc, inputs, initial_soc, simlist):
        # For casadi we do not use multiple actors but instead the integrator
        # function that is generated by casadi handles multithreading behind
        # the scenes
        tic = ticker.time()

        self.actors = [GenericActor()]
        for a in self.actors:
            a.setup(
                Nspm=self.spm_per_worker,
                sim_func=self.sim_func,
                parameter_values=self.parameter_values,
                dt=self.dt,
                inputs=inputs,
                variable_names=self.variable_names,
                initial_soc=initial_soc,
                nproc=nproc,
                simlist=simlist,
            )
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor setup in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def step_actors(self):
        tic = ticker.time()
        events = self.actors[0].step(self.build_inputs()[0])
        if events:
            self.log_event()
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor stepped in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def evaluate_actors(self):
        tic = ticker.time()
        self.actors[0].evaluate(self.build_inputs()[0])
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor evaluated in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def get_actor_output(self, step):
        tic = ticker.time()
        self.output[:, step, :] = self.actors[0].output()
        toc = ticker.time()
        lp.logger.info(
            "Casadi actor output got in time " + str(np.around(toc - tic, 3)) + "s"
        )

    def log_event(self):
        event_change = np.asarray(self.actors[0].get_event_change())
        Nr, Nc = event_change.shape
        event_names = self.actors[0].get_event_names()
        for r in range(Nr):
            if np.any(event_change[r, :]):
                lp.logger.warning(
                    event_names[r]
                    + ", Batteries: "
                    + str(np.where(event_change[r, :])[0].tolist())
                )

    def cleanup(self):
        pass
