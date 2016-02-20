# This file is part of atooms
# Copyright 2010-2014, Daniele Coslovich

"""Simulation base class handling callbacks logic"""

import sys
import os
import time
import logging
import copy

from atooms.utils import rank, size

# Logging facilities

class ParallelFilter(logging.Filter):
    def filter(self, rec):
        if hasattr(rec, 'rank'):
            if rec.rank == 'all':
                return True
            else:
                return rank == rec.rank
        else:
            return rank == 0

log = logging.getLogger('atooms')
#formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')
#handler = logging.StreamHandler()
#handler.setFormatter(formatter)
log.addFilter(ParallelFilter())
#log.addHandler(handler)
log.setLevel(logging.INFO)

# Default exceptions

class SimulationEnd(Exception):
    pass

class WallTimeLimit(Exception):
    pass

class SchedulerError(Exception):
    pass

# Default observers

# Different approaches are possible:
# 1. use callable classes passing args to __init__ and interval to add()
#    Exemple of client code:
#      sim.add(simulation.WriterThermo(), interval=100)
#      sim.add(simulation.TargetRMSD(5.0))

# 2. use functions passing interval and args as kwargs 
#    args are then passed to the function upon calling
#    In this case, we should differentiate types of callbacks via different add() methods
#    Exemple of client code:
#      sim.add(simulation.writer_thermo, interval=100)
#      sim.add(simulation.target_rmsd, rmsd=5.0)

# At least for default observers, it should be possible to use shortcuts
#   sim.writer_thermo_interval = 100
#   sim.target_rmsd = 5.0
# which will then add / modify the callbacks

# We identify callbacks by some types
# * target : these callbacks raise a SimualtionEnd when it's over
# * writer : these callbacks dump useful stuff to file
# and of course general purpose callback can be passed to do whatever

# It os the backend's responsibility to implement specific Writer observers.

class WriterThermo(object):
    def __call__(self, e):
        log.debug('thermo writer')

class WriterConfig(object):
    def __call__(self, e):
        log.debug('config writer')

class WriterCheckpoint(object):
    def __call__(self, e):
        log.debug('checkpoint writer')

class Target(object):

    def __init__(self, name, target):
        self.name = name
        self.target = target

    def __call__(self, sim):
        x = float(getattr(sim, self.name))
        log.debug('targeting %s to %g [%d]' % (self.name, x, int(float(x) / self.target * 100)))
        if x >= self.target:
            raise SimulationEnd('achieved target %s: %s' % (self.name, self.target))

    def fraction(self, sim):
        """Fraction of target value already achieved"""
        return float(getattr(sim, self.name)) / self.target

class TargetSteps(Target):

    # Note: this class is there just as an insane proof of principle
    # Steps targeting can/should be implemented by checking a simple int variable
    def __init__(self, target):
        Target.__init__(self, 'steps', target)

class TargetRMSD(Target):

    def __init__(self, target):
        Target.__init__(self, 'rmsd', target)

class TargetWallTime(Target):

    def __init__(self, wall_time):
        self.wtime_limit = wall_time

    def __call__(self, sim):
        if sim.elapsed_wall_time() > self.wtime_limit:
            raise WallTimeLimit('target wall time reached')
        else:
            t = sim.elapsed_wall_time()
            dt = self.wtime_limit - t
            log.debug('elapsed time %g, reamining time %g' % (t, dt))

class UserStop(object):
    """Allows a user to stop the simulation smoothly by touching a STOP
    file in the output root directory.
    Currently the file is not deleted to allow parallel jobs to all exit.
    """
    def __call__(self, e):
        # To make it work in parallel we should broadcast and then rm 
        # or subclass userstop in classes that use parallel execution
        if self.STORAGE == 'directory':
            log.debug('User Stop %s/STOP' % e.output_path)
            # TODO: support files as well
            if os.path.exists('%s/STOP' % e.output_path):
                raise SimulationEnd('user has stopped the simulation')
        else:
            raise RuntimeError('USerStop wont work atm with file storage')

#TODO: interval can be a function to allow non linear sampling
class Scheduler(object):

    """Scheduler to call observer during the simulation"""

    def __init__(self, interval, calls=None, target=None):
        self.interval = interval
        self.calls = calls
        self.target = target
        # Main switch
        if interval is not None:
            # Fixed interval.
            self.interval = interval
        else:
            if calls is not None:
                # Fixed number of calls.
                if self.target is not None:
                    # If both calls and target are not None, we determine interval
                    self.interval = max(1, self.target / self.calls)
                else:
                    # Dynamic scheduling
                    raise SchedulerError('dynamic scheduling not implemented')
            else:
                # Scheduler is disabled
                self.interval = sys.maxint

    def next(self, this):
        return (this / self.interval + 1) * self.interval

    def now(self, this):
        return this % self.interval == 0


class Simulation(object):

    """Simulation abstract class using callback support."""

    # TODO: write initial configuration as well

    # Comvoluted trick to allow subclass to use custom observers for
    # target and writer without overriding setup(): have class
    # variables to point to the default observer classes that may be
    # switched to custom ones in subclasses
    _TARGET_STEPS = TargetSteps
    _TARGET_RMSD = TargetRMSD
    _WRITER_THERMO = WriterThermo
    _WRITER_CONFIG = WriterConfig    
    _WRITER_CHECKPOINT = WriterCheckpoint

    # Storage class attribute: backends can either dump to a directory
    # or following a file suffix logic. Meant to be subclassed.
    STORAGE = 'directory'

    def __init__(self, initial_state, output_path, 
                 steps=None, rmsd=None,
                 thermo_interval=None, thermo_number=None, 
                 config_interval=None, config_number=None,
                 checkpoint_interval=None, checkpoint_number=None,
                 restart=False):
        """We expect input and output paths as input.
        Alternatively, input might be a system (or trajectory?) instance.
        """
        self._callback = []
        self._scheduler = []
        self.steps = 0
        self.initial_steps = 0
        self.start_time = time.time()
        self.restart = restart
        self.output_path = output_path # can be file or directory
        self.system = initial_state
        # Store a copy of the initial state to calculate RMSD
        self.initial_system = copy.deepcopy(self.system)
        # Setup schedulers and callbacks
        self.target_steps = steps
        # Target steps are checked only at the end of the simulation
        if self.target_steps:
            self.add(self._TARGET_STEPS(self.target_steps), Scheduler(self.target_steps))
        # TODO: rmsd targeting interval is hard coded
        if rmsd:
            self.add(self._TARGET_RMSD(target_rmsd), Scheduler(10000))

        # TODO: implement dynamic scheduling or fail when interval is None and targeting is rmsd
        if thermo_interval or thermo_number:
            self.add(self._WRITER_THERMO(), Scheduler(thermo_interval, thermo_number, self.target_steps))
        if config_interval or config_number:
            self.add(self._WRITER_CONFIG(), Scheduler(config_interval, config_number, self.target_steps))
        # Checkpoint must be after other writers
        if checkpoint_interval or checkpoint_number:
            self.add(self._WRITER_CHECKPOINT(), Scheduler(checkpoint_interval, checkpoint_number, self.target_steps))

        # If we are not targeting steps, we set it to the largest possible int
        # TODO: can we drop this?
        if self.target_steps is None:
            self.target_steps = sys.maxint

    def setup(self, 
              target_steps=None, target_rmsd=None,
              thermo_period=None, thermo_number=None, 
              config_period=None, config_number=None,
              checkpoint_period=None, checkpoint_number=None,
              reset=False):
        raise RuntimeError('use of deprecated setup() function')

    @property
    def rmsd(self):
        # TODO: provide rmsd by species 07.12.2014
        return self.system.mean_square_displacement(self.initial_system)**0.5

    def add(self, callback, scheduler):
        """Add an observer (callback) to be called along a scheduler"""
        self._callback.append(callback)
        self._scheduler.append(scheduler)
        # TODO: enforce checkpoint being last
        
    def report(self):
        for f, s in zip(self._callback, self._scheduler):
            log.info('Observer %s: interval=%s calls=%s target=%s' % (type(f), s.interval, s.calls, s.target))

    def notify(self, condition=lambda x : True): #, callback, scheduler):
        for f, s in zip(self._callback, self._scheduler):
            try:
                # TODO: this check should be done internally by observer
                if s.now(self.steps) and condition(f):
                    f(self)
            except SchedulerError:
                log.error('error with %s' % f, s.interval, s.calls)
                raise
    
    def elapsed_wall_time(self):
        return time.time() - self.start_time

    def wall_time_per_step(self):
        """Return the wall time in seconds per step.
        Can be conventiently subclassed by more complex simulation classes."""
        return self.elapsed_wall_time() / (self.steps-self.initial_steps)

    # Our template consists of three steps: pre, until and end
    # Typically a backend will implement the until method.
    # It is recommended to *extend* (not override) the base run_pre() in subclasses
    # TODO: when should checkpoint be read? The logic must be set here
    # Having a read_checkpoint() stub method would be OK here.
    def run_pre(self):
        """This should be safe to called by subclasses before or after reading checkpoint"""
        pass
            
    def run_until(self, n):
        # Design: it is run_until responsability to set steps: self.steps = n
        # bear it in mind when subclassing 
        pass

    def run_end(self):
        pass

    def run(self, target_steps=None):
        if target_steps is not None:
            # If we ask for more steps on the go, it means we are restarting
            if target_steps > self.target_steps:
                self.restart = True
            self.target_steps = target_steps

        try:
            self.run_pre()
            self.initial_steps = copy.copy(self.steps)
            self.report()
            # Before entering the simulation, check if we can quit right away
            # TODO: find a more elegant way to notify targeters only / order observers
            self.notify(lambda x : isinstance(x, Target))
            if not self.restart:
                self.notify(lambda x : not isinstance(x, Target))
            log.info('simulation started at %d' % self.steps)
            log.info('targeted number of steps: %s' % self.target_steps)
            while True:
#                if self.steps >= self.target_steps:
#                    raise SimulationEnd('target steps achieved')
                # Run simulation until any of the observers need to be called
                next_step = min([s.next(self.steps) for s in self._scheduler])
                self.run_until(next_step)
                self.steps = next_step
                # TODO: log should be done only by rank=0 in parallel: encapsulate
                log.debug('step=%d/%d wtime/step=%.2g' % (self.steps, self.target_steps,
                                                         self.wall_time_per_step()))
                # Notify writer and generic observers before targeters
                self.notify(lambda x : not isinstance(x, Target))
                self.notify(lambda x : isinstance(x, Target))

        except SimulationEnd as s:
            # TODO: make checkpoint is an explicit method of simulation and not a callback! 16.12.2014
            # The rationale here is that notify will basically check for now() but writecheckpoint must
            # here we must call no matter how the period fits. The problem was that PT checkpoint
            # was not a subclass of base one, and so it was not called!
            for f in self._callback:
                if isinstance(f, WriterCheckpoint):
                    f(self)
            #self.notify(lambda x : isinstance(x, WriterCheckpoint))
            if not self.initial_steps == self.steps:
                log.info('wall time [s]: %.1f' % self.elapsed_wall_time())
                log.info('steps/wall time [1/s]: %.1f' % (1./self.wall_time_per_step()))
            log.info('simulation ended successfully: %s' % s.message)
            self.run_end()

        finally:
            log.info('exiting')

