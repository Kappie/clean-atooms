# This file is part of atooms
# Copyright 2010-2014, Daniele Coslovich

import numpy
import h5py
from atooms import ndim
from atooms.trajectory import TrajectoryBase
from atooms.system import System
from atooms.system.particle import Particle
from atooms.system.cell import Cell
from atooms.interaction.interaction import Interaction
from atooms.potential.potential import PairPotential
from atooms.potential.cutoff import CutOff

# TODO: decorate hdf5 class so that error messages contain the path of the offending file (at least!)
class _SafeFile(h5py.File):
    # TODO: recursively create all h5 groups that do not exist
    # TODO: actually redefine create_group unless this is a serious performace issue ?
    def create_group_safe(self, group):
        if not group in self:
            self.create_group(group)

def _get_cached_list_h5(fh, h5g, data):
    """ Replace |data| with read data in group |h5g| only if data is None """
    if data is None:
        try:
            data = [entry[0] for entry in fh[h5g].values()]
        except:
            data = []
    return data
           
class TrajectoryHDF5(TrajectoryBase):

    """ Trajectory layout based on HDF5 libraries """

    suffix = 'h5'

    def __init__(self, filename, mode='r'):
        TrajectoryBase.__init__(self, filename, mode)

        self.general_info = {}
        self._grandcanonical = False
        self._system = None

        if self.mode == 'r' or self.mode == 'r+':
            self.trajectory = h5py.File(self.filename, mode)
            # gather general info on file
            for entry in self.trajectory['/']:
                if type(self.trajectory[entry]) == h5py.highlevel.Dataset:
                    self.general_info[entry] = self.trajectory[entry]
            # get steps list (could be cached and put in init_read())
            self.steps = [d[0] for d in self.trajectory['trajectory/realtime/stepindex'].values()]
            self._block_period = self.read_blockperiod()
            # private list of samples. This solves the problem that samples may start from 0
            # or 1 depending on the code that initially produced the data
            self._samples = [d[0] for d in self.trajectory['trajectory/realtime/sampleindex'].values()]
            
        elif self.mode == 'w' or self.mode == 'r+' or self.mode == "w-":
            self.trajectory = _SafeFile(self.filename, self.mode)

        else:
            raise ValueError('Specify mode (r/w) for file %s (invalid: %s)' % (self.filename, self.mode))

    def close(self):
        self.trajectory.close()

    def read_timestep(self):
        return self.trajectory['trajectory/realtime/timestep'][0]

    def write_timestep(self, value):
        self.trajectory.create_group_safe('/trajectory')
        self.trajectory.create_group_safe('/trajectory/realtime')
        self.trajectory['trajectory/realtime/timestep'] = [value]

    def read_blockperiod(self):
        try:
            return self.trajectory['trajectory/realtime/block_period'][0]
        except:
            return None

    def write_blockperiod(self, value):
        self.trajectory.create_group_safe('/trajectory')
        self.trajectory.create_group_safe('/trajectory/realtime')
        self.trajectory['trajectory/realtime/block_period'] = [value]

    def write_init(self, system):
        self.trajectory.create_group_safe('/initialstate')
        self.trajectory['DIMENSIONS'] = [3]
        self.trajectory['NAME_SYS'] = ['Unknown'] #system.name
        self.trajectory['VERSION_TRJ'] = ['1.2']
        self.trajectory['VERSION_MD'] = ['X.X.X']

        # Particles
        group = '/initialstate/particle/'
        if system.particle:
            self.trajectory.create_group_safe(group)
            particle = system.particle

            # Check that species id are ok (problems might arise when converting from RUMD
            if min([p.id for p in particle]) < 1:
                raise ValueError('Particles ids are smaller than 1. Use NormalizeId decorator to fix this.')
 
            particle_h5 = {'number_of_species': [len(list(set([p.id for p in particle])))], #particle.numberSpecies(),
                           'number_of_particles': [len(particle)],
                           'identity': [p.id for p in particle],
                           'element': ['%3s' % p.name for p in particle],
                           'mass': [p.mass for p in particle],
                           'position': [p.position for p in particle],
                           'velocity': [p.velocity for p in particle],
                           }
            # TODO: to create several datasets within a group, make this a function and pass a dict
            for name, dataset in particle_h5.items():
                self.trajectory[group + name] = dataset
        # Matrix
        group = '/initialstate/matrix/'
        if system.matrix:
            self.trajectory.create_group_safe(group)
            matrix = system.matrix
            matrix_h5 = {'type' : [''],
                         'id' : [0],
                         'number_of_species': [len(list(set([p.id for p in matrix])))],
                         'number_of_particles': [len(matrix)],
                         'identity': [p.id for p in matrix],
                         'element': ['%3s' % p.name for p in matrix],
                         'mass': [p.mass for p in matrix],
                         'position': [p.position for p in matrix],
                         }
            for name, dataset in matrix_h5.items():
                self.trajectory[group + name] = dataset
        # Cell
        group = '/initialstate/cell/'
        if system.cell:
            self.trajectory.create_group_safe(group)
            self.trajectory[group + 'sidebox'] = system.cell.side

        # Thermostat
        group = '/initialstate/thermostat/'
        if system.thermostat:
            self.trajectory.create_group_safe(group)
            self.trajectory[group + 'temperature'] = system.thermostat.temperature
            self.trajectory[group + 'type'] = system.thermostat.name
            self.trajectory[group + 'mass'] = system.thermostat.mass
            self.trajectory[group + 'collision_period'] = system.thermostat.collision_period

        # Interaction
        # TODO: we should make sure interaction is a list
        group = '/initialstate/interaction/'
        if system.interaction:
            self.write_interaction([system.interaction])
        
    def write_interaction(self, interaction):
        rgr = '/initialstate/interaction/'
        self.trajectory.create_group_safe('/initialstate/')
        # If the group exisist we delete it. This does not actual clear space in h5 file.
        # We could do it on a dataset basis via require_dataset, or visit the group and delete everything.
        try:
            del self.trajectory['/initialstate/interaction/']
        except:
            pass
        self.trajectory.create_group_safe('/initialstate/interaction/')
        self.trajectory[rgr + 'number_of_interactions'] = [len(interaction)]
        for i, term in enumerate(interaction):
            igr = rgr + '/interaction_%d/' % (i+1)
            self.trajectory.create_group_safe(igr) # numbering is from one to comply with atooms
            self.trajectory[igr + 'interaction_type'] = [term.name]
            self.trajectory[igr + 'number_of_potentials'] = [len(term.potential)]
            for j, phi in enumerate(term.potential):
                pgr = igr + '/potential_%d/' % (j+1)
                self.trajectory.create_group_safe(pgr)
                self.trajectory[pgr + 'potential'] = [phi.name]
                self.trajectory[pgr + 'interacting_bodies'] = [phi.interacting_bodies]
                self.trajectory[pgr + 'interacting_species'] = phi.species
                self.trajectory[pgr + 'parameters_number'] = [len(phi.params)]
                self.trajectory[pgr + 'parameters_name'] = sorted(phi.params.keys())
                self.trajectory[pgr + 'parameters'] = [phi.params[k] for k in sorted(phi.params.keys())]
                self.trajectory[pgr + 'cutoff_scheme'] = [phi.cutoff.name]
                self.trajectory[pgr + 'cutoff_radius'] = [phi.cutoff.formal_radius]
                self.trajectory[pgr + 'lookup_points'] = [phi.npoints]

    def write_sample(self, system, step, ignore = []):
        self.trajectory.create_group_safe('/trajectory')
        self.trajectory.create_group_safe('/trajectory/realtime')
        self.trajectory.create_group_safe('/trajectory/realtime/stepindex')
        self.trajectory.create_group_safe('/trajectory/realtime/sampleindex')

        sample = len(self.steps) + 1
        csample = '/sample_%7.7i' % sample

        # TODO: check that sample was not already written, make it a general debug decoration for hdf5
        self.trajectory['/trajectory/realtime/stepindex' + csample] = [step]
        self.trajectory['/trajectory/realtime/sampleindex' + csample] = [sample]

        if system.particle != None:
            self.trajectory.create_group_safe('/trajectory/particle')
            self.trajectory.create_group_safe('/trajectory/particle/position')
            self.trajectory.create_group_safe('/trajectory/particle/velocity')
            if not 'position' in ignore: self.trajectory['/trajectory/particle/position' + csample] = [p.position for p in system.particle]
            if not 'velocity' in ignore: self.trajectory['/trajectory/particle/velocity' + csample] = [p.velocity for p in system.particle]

        if system.cell != None:
            self.trajectory.create_group_safe('/trajectory/cell')
            self.trajectory.create_group_safe('/trajectory/cell/sidebox')
            self.trajectory['/trajectory/cell/sidebox' + csample] = [system.cell.side]

    def read_init(self):
        name = 'Unknown'

        # read particles
        group = self.trajectory['/initialstate/particle']
        n = self.trajectory['/initialstate/particle/number_of_particles'].value[0]
        for entry in group:
            if entry == 'identity': spe = group[entry][:]
            if entry == 'element' : ele = group[entry][:]
            if entry == 'mass'    : mas = group[entry][:]
            if entry == 'position': pos = group[entry][:]
            if entry == 'velocity': vel = group[entry][:]
        particle = [ Particle(spe[i],ele[i],mas[i],pos[i,:],vel[i,:]) for i in range(n) ] 

        # read cell
        group = self.trajectory['/initialstate/cell']
        for entry in group:
            if entry == 'sidebox' : sidebox = group[entry][:]
        cell = Cell(sidebox)

        # TODO: read actual interactions from file if they are present
        # create some default total interaction
        interaction = Interaction('', [])

        # build system
        self._system = System(name, particle, cell, interaction)

        # read matrix
        if 'matrix' in  self.trajectory['/initialstate']:
            group = self.trajectory['/initialstate/matrix']
            for entry in group:
                if entry == 'identity': spe = group[entry][:]
                if entry == 'element' : ele = group[entry][:]
                if entry == 'mass'    : mas = group[entry][:]
                if entry == 'position': pos = group[entry][:]
            matrix = [ Particle(spe[i],ele[i],mas[i],pos[i,:]) for i in range(len(spe)) ] 
            self._system.add_porous_matrix(matrix)

        return self._system

    def read_interaction(self):
        # read interaction terms
        if not 'interaction' in self.trajectory['/initialstate']:
            return None

        group = self.trajectory['/initialstate/interaction']
        n = self.trajectory['/initialstate/interaction/number_of_interactions'][0]
        interactions = []
        for i in range(n):
            g = '/initialstate/interaction/interaction_%d/' % (i+1)
            np = self.trajectory[g + 'number_of_potentials'][0]
            name = self.trajectory[g + 'interaction_type'][0]
            potentials = []
            for j in range(np):
                sg = self.trajectory[g + 'potential_%d/' % (j+1)]
                #params = {k:v for k, v in zip(sg['parameters_name'][:], sg['parameters'][:])}
                # make it compatible with 2.6
                params = {}
                for k, v in zip(sg['parameters_name'][:], sg['parameters'][:]):
                    params[k] = v
                p = PairPotential(sg['potential'][0], params, sg['interacting_species'][:], 
                                  CutOff(sg['cutoff_scheme'][0], sg['cutoff_radius'][0]),
                                  sg['lookup_points'][0])

                potentials.append(p)
            interactions.append(Interaction(name, potentials))
        return interactions

    def read_sample(self, sample, unfolded=False):
        # We must increase sample by 1 if we iterate over samples with len().
        # This is some convention to be fixed once and for all
        isample = self._samples[sample]
        csample = '/sample_%7.7i' % isample
        # read particles
        group = self.trajectory['/trajectory/particle']
        if unfolded:
            if not 'position_unfolded' in group:
                raise NotImplementedError('cannot unfold like this, use decorator instead')
                # self._unfold()
                # TODO: fix it since sample may not be [0,1,2,...]
                #print sample, len(self._pos_unf)
                pos = self._pos_unf[sample]
            else:
                # fix for unfolded positions that were not written at the first step
                # should be fixed once and for all in md.x
                if sample == 0:
                    pos = self.trajectory['/initialstate/particle/position'][:]
                else:
                    pos = group['position_unfolded' + csample][:]
        else:
            pos = group['position' + csample][:]

        try:
            vel = group['velocity' + csample][:]
        except:
            vel = numpy.zeros([len(pos),ndim])

        # TODO: now make an explicit copy because copy creates huge leakage... try with deepcopy?
        p = []
        for r, v in zip(pos, vel):
            p.append(Particle(position=r, velocity=v))

        # TODO: optimize, this takes quite some additional time, almost x2
        for pi, r in zip(p, self._system.particle):
            pi.id = r.id
            pi.mass = r.mass
            pi.name = r.name

        # Read also interaction.
        has_int = True
        try:
            group = self.trajectory['/trajectory/interaction']
        except:
            has_int = False

        if has_int:
            self._system.interaction.total_energy = group['energy' + csample][0]
            self._system.interaction.total_virial = group['virial' + csample][0]
            self._system.interaction.total_stress = group['stress' + csample][:]
        
        return System('', p, self._system.cell, self._system.interaction)