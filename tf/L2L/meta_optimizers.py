from __future__ import print_function
from abc import ABCMeta
import tensorflow as tf
from tensorflow.python.util import nest
import pickle
from preprocess import Preprocess


class Meta_Optimizer():
    __metaclass__ = ABCMeta

    global_args = None
    io_handle = None
    problem = None
    optimizer = None
    second_derivatives = None
    preprocessor = None
    preprocessor_args = None
    debug_info = None
    trainable_variables = None
    def __init__(self, problem, path, args):
        if path is not None:
            print('Loading optimizer args, ignoring provided args...')
            self.global_args = self.load_args(path)
            print('Args Loaded, call load_optimizer with session to restore the optimizer graph.')
        else:
            self.global_args = args
        self.problem = problem
        if 'preprocess' in self.global_args and self.global_args['preprocess'] is not None:
            self.preprocessor = self.global_args['preprocess'][0]
            self.preprocessor_args = self.global_args['preprocess'][1]
        self.second_derivatives = self.global_args['second_derivatives'] if 'second_derivatives' in self.global_args else False
        self.learning_rate = tf.get_variable('learning_rate', initializer=tf.constant(self.global_args['learning_rate']
                                                                                      if 'learning_rate' in self.global_args
                                                                                      else .0001,
                                                                                      dtype=tf.float32), trainable=False)
        self.optimizer = tf.train.AdamOptimizer(self.global_args['meta_learning_rate'] if 'meta_learning_rate' in
                                                                                          self.global_args else .01)
        self.debug_info = []
        self.trainable_variables = []

    def __init_trainable_vars_list(self):
        self.trainable_variables = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)

    def __init_io_handle(self):
        self.io_handle = tf.train.Saver(self.trainable_variables, max_to_keep=100)

    def end_init(self):
        self.__init_trainable_vars_list()
        self.__init_io_handle()

    def preprocess_input(self, inputs):
        if self.preprocessor is not None:
            return self.preprocessor(inputs, self.preprocessor_args)
        else:
            return inputs
    
    def is_availble(self, param, args=None):
        args = self.global_args if args is None else args
        return param in args and args[param] is not None

    def flatten_input(self, i, inputs):
        return tf.reshape(inputs, [self.problem.variables_flattened_shape[i], 1])

    def get_gradients_raw(self, variables):
        return tf.gradients(self.problem.loss(variables), variables)

    def get_flattened_gradients(self, variables):
        gradients = self.get_gradients_raw(variables)
        if not self.second_derivatives:
            gradients = [tf.stop_gradient(gradient) for gradient in gradients]
        gradients = [self.flatten_input(i, gradient) for i, gradient in enumerate(gradients)]
        return gradients

    def get_preprocessed_gradients(self, variables):
        return [self.preprocess_input(gradient) for gradient in self.get_flattened_gradients(variables)]

    @property
    def meta_optimizer_input_stack(self):
        variables = self.problem.variables
        gradients_raw = self.get_gradients_raw(variables)
        flat_gradients = [self.flatten_input(i, gradient) for i, gradient in enumerate(gradients_raw)]
        preprocessed_gradients = [self.preprocess_input(gradient) for gradient in flat_gradients]
        stacked_inputs = [{
            'x': variable,
            'gradient_raw': gradients_raw,
            'flat_gradient': flat_gradient,
            'preprocessed_gradient': preprocessed_gradient
        }
            for (variable, gradient_raw, flat_gradient, preprocessed_gradient) in
            zip(variables, gradients_raw, flat_gradients, preprocessed_gradients)]
        return stacked_inputs

    def updates(self, args):
        pass

    def core(self, inputs):
        pass

    def loss(self, variables):
        pass

    def step(self):
        pass

    def minimize(self, loss):
        return self.optimizer.minimize(loss)

    def build(self):
        pass

    def reset_optimizer(self):
        return [tf.variables_initializer(self.trainable_variables)]

    def reset_problem(self):
        return [tf.variables_initializer(self.problem.variables + self.problem.constants)]

    @staticmethod
    def load_args(path):
        pickled_args = pickle.load(open(path + '_config.p', 'rb'))
        pickled_args['preprocess'][0] = getattr(Preprocess, pickled_args['preprocess'][0])
        return pickled_args

    def save_args(self, path):
        self.global_args['preprocess'][0] = self.global_args['preprocess'][0].func_name
        pickle.dump(self.global_args, open(path + '_config.p', 'wb'))
        self.global_args['preprocess'][0] = getattr(Preprocess, self.global_args['preprocess'][0])

    def load(self, sess, path):
        self.io_handle.restore(sess, path)
        print('Optimizer Restored')

    def save(self, sess, path):
        print('Saving optimizer')
        self.io_handle.save(sess, path)
        self.save_args(path)

class l2l(Meta_Optimizer):

    state_size = None
    W, b = None, None
    lstm = None
    fx_array = None

    @property
    def meta_optimizer_input_stack(self):
        inputs = super(l2l, self).meta_optimizer_input_stack
        for (input, hidden_state) in zip(inputs, self.hidden_states):
            input['hidden_state'] = hidden_state
        return inputs

    def __init__(self, problem, path, args):
        super(l2l, self).__init__(problem, path, args)
        self.state_size = self.global_args['state_size']
        self.num_layers = self.global_args['num_layers']
        self.unroll_len = self.global_args['unroll_len']
        self.meta_optimizer = tf.train.AdamOptimizer(self.global_args['meta_learning_rate'])
        self.fx_array = tf.TensorArray(tf.float32, size=self.unroll_len, clear_after_read=False)

        # initialize for later use.
        with tf.variable_scope('optimizer_core'):
            # Formulate variables for all states as it allows to use tf.assign() for states
            def get_states(batch_size):
                state_variable = []
                for state_c, state_h in self.lstm.zero_state(batch_size, tf.float32):
                    state_variable.append(tf.contrib.rnn.LSTMStateTuple(tf.Variable(state_c, trainable=False),
                                                                        tf.Variable(state_h, trainable=False)))
                return tuple(state_variable)

            self.lstm = tf.contrib.rnn.BasicLSTMCell(self.state_size)
            self.lstm = tf.contrib.rnn.MultiRNNCell(
                [tf.contrib.rnn.BasicLSTMCell(self.state_size) for _ in range(self.num_layers)])
            gradients = self.preprocess_input(self.get_flattened_gradients(self.problem.variables)[0])

            with tf.variable_scope('hidden_states'):
                self.hidden_states = [get_states(shape) for shape in self.problem.variables_flattened_shape]

            with tf.variable_scope('rnn_init'):
                self.lstm(gradients, self.hidden_states[0])

            with tf.variable_scope('rnn_linear'):
                self.W = tf.get_variable('softmax_w', [self.state_size, 1])
                self.b = tf.get_variable('softmax_b', [1])

        self.end_init()

    def core(self, inputs):
        with tf.variable_scope('optimizer_core/rnn_init', reuse=True):
            lstm_output, hidden_state = self.lstm(inputs['preprocessed_gradient'], inputs['hidden_state'])
        deltas = tf.add(tf.matmul(lstm_output, self.W, name='output_matmul'), self.b, name='add_bias')
        return [deltas, hidden_state]

    def step(self):
        def update(t, fx_array, params, hidden_states, deltas_list):
            rnn_inputs = self.preprocess_input(self.get_flattened_gradients(params))
            for i, (rnn_input, hidden_state) in enumerate(zip(rnn_inputs, hidden_states)):
                deltas, hidden_states[i] = self.core({'preprocessed_gradient': rnn_input, 'hidden_state': hidden_state})
                # overwrite each iteration of the while loop, so you will end up with the last update
                deltas_list[i] = deltas
                deltas = tf.reshape(deltas, self.problem.variables[i].get_shape(), name='reshape_deltas')
                deltas = tf.multiply(deltas, self.learning_rate, 'multiply_deltas')
                params[i] = tf.add(params[i], deltas, 'add_deltas_params')
            fx_array = fx_array.write(t, self.problem.loss(params))
            t_next = t + 1
            return t_next, fx_array, params, hidden_states, deltas_list

        deltas_list = list(range(len(self.hidden_states)))

        _, self.fx_array, x_next, h_next, deltas_list = tf.while_loop(
            cond=lambda t, *_: t < self.unroll_len,
            body=update,
            loop_vars=([0, self.fx_array, self.problem.variables, self.hidden_states, deltas_list]),
            parallel_iterations=1,
            swap_memory=True,
            name="unroll")

        return {'x_next': x_next, 'h_next': h_next, 'deltas': deltas_list}

    def updates(self, args):
        update_list = list()
        update_list.append([tf.assign(variable, variable_final) for variable, variable_final in
                              zip(self.problem.variables, args['x_next'])])
        update_list.append([tf.assign(hidden_state, hidden_state_final) for hidden_state, hidden_state_final in
                              zip(nest.flatten(self.hidden_states), nest.flatten(args['h_next']))])
        return update_list

    def reset_problem(self):
        reset = super(l2l, self).reset_problem()
        reset.append(nest.flatten(self.hidden_states))
        reset.append(self.fx_array.close())
        return reset

    def loss(self, variables):
        return tf.divide(tf.reduce_sum(self.fx_array.stack()), self.unroll_len)

    def build(self):
        step = self.step()
        updates = self.updates(step)
        loss = self.loss(step['x_next'])
        meta_step = self.minimize(loss)
        reset = [self.reset_problem(), self.reset_optimizer()]
        return step, updates, loss, meta_step, reset



class MlpSimple(Meta_Optimizer):

    w_1, b_1, w_out, b_out = None, None, None, None
    layer_width = None
    hidden_layers = None
    def __init__(self, problem, path, args):
        super(MlpSimple, self).__init__(problem, path, args)
        input_dim, output_dim = (1, 1)
        if 'dims' in args:
            input_dim, output_dim = args['dims']
        elif 'preprocess' in args and args['preprocess'] is not None:
            input_dim = 2
        self.num_layers = 2
        self.layer_width = self.global_args['layer_width'] if self.is_availble('layer_width') else 20
        init = tf.random_normal_initializer(mean=0.0, stddev=.1)
        with tf.variable_scope('optimizer_core'):
            self.w_1 = tf.get_variable('w_in', shape=[input_dim, self.layer_width], initializer=init)
            self.b_1 = tf.get_variable('b_in', shape=[1, self.layer_width], initializer=tf.zeros_initializer)
            if self.is_availble('hidden_layers') and self.global_args['hidden_layers']:
                self.hidden_layers = []
                for layer in range(self.global_args['hidden_layers']):
                    weight = tf.get_variable('w_' + str(layer + 1), shape=[self.layer_width, self.layer_width], initializer=init)
                    bias = tf.get_variable('b_' + str(layer + 1), shape=[1, self.layer_width], initializer=init)
                    self.hidden_layers.append([weight, bias])
            self.w_out = tf.get_variable('w_out', shape=[self.layer_width, output_dim], initializer=init)
            self.b_out = tf.get_variable('b_out', shape=[1, output_dim], initializer=tf.zeros_initializer)
        self.end_init()

    def core(self, inputs):
        activations = tf.nn.softplus(tf.add(tf.matmul(inputs['preprocessed_gradient'], self.w_1), self.b_1))
        if self.hidden_layers is not None:
            for i, layer in enumerate(self.hidden_layers):
                activations = tf.nn.softplus(tf.add(tf.matmul(activations, layer[0]), layer[1]), name='layer_' + str(i))
        output = tf.add(tf.matmul(activations, self.w_out), self.b_out, name='layer_final_activation')
        return [output]

    def step(self):
        x_next = list()
        deltas_list = []
        preprocessed_gradients = self.get_preprocessed_gradients(self.problem.variables)
        optimizer_inputs = preprocessed_gradients
        for i, (variable, optim_input) in enumerate(zip(self.problem.variables, optimizer_inputs)):
            deltas = self.core({'preprocessed_gradient': optim_input})[0]
            deltas_list.append(deltas)
            deltas = tf.multiply(deltas, self.learning_rate, name='apply_learning_rate')
            deltas = tf.reshape(deltas, variable.get_shape(), name='reshape_deltas')
            x_next.append(tf.add(variable, deltas))
        return {'x_next': x_next, 'deltas': deltas_list}

    def updates(self, args):
        update_list = [tf.assign(variable, updated_var) for variable, updated_var in zip(self.problem.variables, args['x_next'])]
        return update_list

    def loss(self, variables):
        return self.problem.loss(variables)

    def build(self):
        step = self.step()
        updates = self.updates(step)
        loss = self.loss(step['x_next'])
        meta_step = self.minimize(loss)
        reset = [self.reset_problem(), self.reset_optimizer()]
        return step, updates, loss, meta_step, reset


class MlpMovingAverage(MlpSimple):

    avg_gradients = None
    def __init__(self, problem, path, args):
        args['dims'] = (4, 1)
        super(MlpMovingAverage, self).__init__(problem, path, args)
        self.avg_gradients = [
            tf.get_variable('avg_gradients_' + str(i), shape=[shape, 1], initializer=tf.zeros_initializer(),
                            trainable=False)
            for i, shape in enumerate(self.problem.variables_flattened_shape)]

    def updates(self, args):
        update_list = super(MlpMovingAverage, self).updates(args)
        gradients = self.get_preprocessed_gradients(args['x_next'])
        update_list.append([tf.assign(avg_gradient, avg_gradient * .9 + .1 * gradient)
                            for gradient, avg_gradient in zip(gradients, self.avg_gradients)])
        return update_list

    def reset_optimizer(self):
        reset = super(MlpMovingAverage, self).reset_optimizer()
        reset.append(tf.variables_initializer(self.avg_gradients))
        return reset

    def reset_problem(self):
        reset = super(MlpMovingAverage, self).reset_problem()
        reset.append(tf.variables_initializer(self.avg_gradients))
        return reset

class MlpGradHistory(MlpSimple):

    gradient_history = None
    gradient_history_ptr = None

    def __init__(self, problem, path, args):
        limit = args['limit']
        args['dims'] = (limit * 2, 1) if self.is_availble('preprocess', args) else (limit, 1)
        super(MlpGradHistory, self).__init__(problem, path, args)
        self.gradient_history_ptr = tf.Variable(0, 'gradient_history_ptr')

    def get_gradient_history(self):
        if self.gradient_history is None:
            gradient_history_tensor = [None for _ in self.problem.variables]
            for history_itr in range(self.global_args['limit']):
                initialized_values = [variable.initialized_value() for variable in self.problem.variables]
                gradients = self.get_preprocessed_gradients(initialized_values)
                for i, gradient in enumerate(gradients):
                    if gradient_history_tensor[i] is None:
                        gradient_history_tensor[i] = gradient
                    else:
                        gradient_history_tensor[i] = tf.concat([gradient_history_tensor[i], gradient], axis=1)
            self.gradient_history = [tf.get_variable('gradients_history' + str(i), initializer=gradient_tensor, trainable=False) 
                                    for i, gradient_tensor in enumerate(gradient_history_tensor)]
        return self.gradient_history

    def core(self, inputs):
        gradients = inputs['preprocessed_gradient']
        cols = 2 if self.is_availble('preprocess') else 1 
        start_ptr = tf.multiply(self.gradient_history_ptr, cols)
        start = tf.slice(gradients, [0, start_ptr], [-1, -1])
        end = tf.slice(gradients, [0, 0], [-1, start_ptr])
        final_input = tf.concat([start, end], 1, 'final_input')
        activations = tf.nn.softplus(tf.add(tf.matmul(final_input, self.w_1), self.b_1))
        if self.hidden_layers is not None:
            for i, layer in enumerate(self.hidden_layers):
                activations = tf.nn.softplus(tf.add(tf.matmul(activations, layer[0]), layer[1]), name='layer_' + str(i))
        output = tf.add(tf.matmul(activations, self.w_out), self.b_out, name='layer_final_activation')
        return [output]

    def step(self):
        x_next = list()
        deltas_list = []
        for i, (variable, variable_gradient_history) in enumerate(zip(self.problem.variables, self.get_gradient_history())):
            deltas = self.core({'preprocessed_gradient': variable_gradient_history})[0]
            deltas_list.append(deltas)
            deltas = tf.multiply(deltas, self.learning_rate, name='apply_learning_rate')
            deltas = tf.reshape(deltas, variable.get_shape(), name='reshape_deltas')
            x_next.append(tf.add(variable, deltas))
        return {'x_next': x_next, 'deltas': deltas_list}
    
    def update_gradient_history_ops(self, variable_ptr, gradients):
        cols = 1
        rows = gradients.shape[0].value
        if len(gradients.shape) > 1:
            cols = gradients.shape[1].value
        write_ptr = tf.multiply(self.gradient_history_ptr, cols)
        indices = []
        for col in range(cols):
            for row in range(rows):
                indices.append([row, write_ptr + col])
        stacked_grads = tf.slice(gradients, [0, 0], [-1, 1])
        for col in range(cols)[1:]:
            stacked_grads = tf.concat([stacked_grads, tf.slice(gradients, [0, col], [-1, 1])], 0)
        return tf.scatter_nd_update(self.gradient_history[variable_ptr], indices, tf.squeeze(stacked_grads))
    #def update_gradient_history_ops(self, variable_ptr, gradients):
    #    indices = [[i, self.gradient_history_ptr] for i in range(gradients.shape[0].value)]
    #    return tf.scatter_nd_update(self.gradient_history[variable_ptr], indices, tf.squeeze(gradients))

    def updates(self, args):
        update_list = super(MlpGradHistory, self).updates(args)
        gradients = self.get_preprocessed_gradients(args['x_next'])
        for i, gradient in enumerate(gradients):
            update_list.append(self.update_gradient_history_ops(i, gradient))
        with tf.control_dependencies(update_list):
            update_itr = tf.cond(self.gradient_history_ptr < self.global_args['limit'] - 1, 
                            lambda: tf.assign_add(self.gradient_history_ptr, 1),
                            lambda: tf.assign(self.gradient_history_ptr, 0))
        return update_list + [update_itr]

    def reset_optimizer(self):
        reset = super(MlpGradHistory, self).reset_optimizer()
        reset.append(tf.variables_initializer(self.gradient_history))
        return reset

    def reset_problem(self):
        reset = super(MlpGradHistory, self).reset_problem()
        reset.append(tf.variables_initializer(self.gradient_history))
        return reset

class MlpXHistory(MlpSimple):

    variable_history = None
    grad_sign_history = None
    history_ptr = None
    update_window = None

    def __init__(self, problem, path, args):
        limit = args['limit']
        args['dims'] = (limit * 2, 1) if self.is_availble('preprocess', args) else (limit, 1)
        super(MlpXHistory, self).__init__(problem, path, args)
        self.history_ptr = tf.Variable(0, 'history_ptr')
        variables, gradients, gradients_sign = None, None, None
        last_variables, last_gradients = None, None
        for history_itr in range(limit):
            if last_variables is None:
                initialized_values = [variable.initialized_value() for variable in self.problem.variables]
                gradients = self.get_flattened_gradients(initialized_values)
                variables = [self.flatten_input(i, variable) for i, variable in enumerate(initialized_values)]
                gradients_sign = [tf.sign(gradient) for gradient in gradients]
                last_variables = variables
                last_gradients = gradients
            else:
                last_variables = [last_variable + .1 * last_gradient for last_variable, last_gradient
                              in zip(last_variables, last_gradients)]
                variables = [tf.concat([variable, new_point], 1) for variable, new_point in zip(variables, last_variables)]
                new_points_reshaped = [tf.reshape(last_variable, variable.get_shape(), name='reshaped_variable')
                                      for last_variable, variable in zip(last_variables, self.problem.variables)]
                last_gradients = self.get_flattened_gradients(new_points_reshaped)
                gradients_sign = [tf.concat([gradient_sign, tf.sign(gradient)], 1) for gradient_sign, gradient in zip(gradients_sign, last_gradients)]

        self.variable_history = [tf.get_variable('var_history' + str(i), initializer=variable_history, trainable=False)
                                 for i, variable_history in enumerate(variables)]
        self.grad_sign_history = [tf.get_variable('grad_sign_history' + str(i), initializer=grad_history, trainable=False)
                                 for i, grad_history in enumerate(gradients_sign)]

    @staticmethod
    def normalize_values(history_tensor):
        max_values = tf.reduce_max(history_tensor, 1)
        min_values = tf.reduce_min(history_tensor, 1)
        max_values = tf.reshape(max_values, [tf.shape(max_values)[0], 1])
        min_values = tf.reshape(min_values, [tf.shape(min_values)[0], 1])
        diff = max_values - min_values + 1e-7
        return (history_tensor - min_values) / diff

    def sort_input(self, inputs):
        start = tf.slice(inputs, [0, self.history_ptr], [-1, -1])
        end = tf.slice(inputs, [0, 0], [-1, self.history_ptr])
        return tf.concat([start, end], 1)

    def core(self, inputs):
        variable_hisory, variable_grad_sign_history = inputs['preprocessed_gradient']
        cols = 2 if self.is_availble('preprocess') else 1
        normalized_variable_history = self.normalize_values(variable_hisory)
        final_var_history = self.sort_input(normalized_variable_history)
        final_var_grad_history = self.sort_input(variable_grad_sign_history)
        final_input = tf.concat([final_var_history, final_var_grad_history], 1)
        activations = tf.nn.softplus(tf.add(tf.matmul(final_input, self.w_1), self.b_1))
        if self.hidden_layers is not None:
            for i, layer in enumerate(self.hidden_layers):
                activations = tf.nn.softplus(tf.add(tf.matmul(activations, layer[0]), layer[1]), name='layer_' + str(i))
        output = tf.tanh(tf.add(tf.matmul(activations, self.w_out), self.b_out, name='layer_final_activation')) / 1.4
        return [output]

    def step(self):
        x_next = list()
        deltas_list = []
        for i, (variable, variable_history, variable_grad_sign_history) in enumerate(zip(self.problem.variables,
                                                                                         self.variable_history,
                                                                                         self.grad_sign_history)):
            deltas = self.core({'preprocessed_gradient': [variable_history, variable_grad_sign_history]})[0]
            deltas_list.append(deltas)
            max_values = tf.reduce_max(variable_history, 1)
            min_values = tf.reduce_min(variable_history, 1)
            max_values = tf.reshape(max_values, [tf.shape(max_values)[0], 1])
            min_values = tf.reshape(min_values, [tf.shape(min_values)[0], 1])
            diff = max_values - min_values + 1e-7
            ref_points = max_values + min_values 
            new_points = ref_points / 2.0 + deltas * diff
            new_points = tf.reshape(new_points, variable.get_shape(), name='reshape_deltas')
            x_next.append(new_points)
        return {'x_next': x_next, 'deltas': deltas_list}

    def update_history_ops(self, variable_ptr, inputs):
        variable, grad_sign = inputs
        history_ops = []
        indices = [[i, self.history_ptr] for i in range(variable.shape[0].value)]
        history_ops.append(tf.scatter_nd_update(self.variable_history[variable_ptr], indices, tf.squeeze(variable)))
        history_ops.append(tf.scatter_nd_update(self.grad_sign_history[variable_ptr], indices, tf.squeeze(grad_sign)))
        return history_ops

    def updates(self, args):
        update_list = super(MlpXHistory, self).updates(args)
        flat_gradients = self.get_flattened_gradients(args['x_next'])
        flat_variables = [self.flatten_input(i, variable) for i, variable in enumerate(args['x_next'])]
        for i, (variable, grads) in enumerate(zip(flat_variables, flat_gradients)):
            new_input = [variable, tf.sign(grads)]
            update_list.extend(self.update_history_ops(i, new_input))
        with tf.control_dependencies(update_list):
            update_itr = tf.cond(self.history_ptr < self.global_args['limit'] - 1,
                            lambda: tf.assign_add(self.history_ptr, 1),
                            lambda: tf.assign(self.history_ptr, 0))
        return update_list + [update_itr]

    def reset_optimizer(self):
        reset = super(MlpXHistory, self).reset_optimizer()
        reset.append(tf.variables_initializer(self.variable_history))
        reset.append(tf.variables_initializer(self.grad_sign_history))
        reset.append(tf.variables_initializer([self.history_ptr]))
        return reset

    def reset_problem(self):
        reset = super(MlpXHistory, self).reset_problem()
        reset.append(tf.variables_initializer(self.variable_history))
        reset.append(tf.variables_initializer(self.grad_sign_history))
        reset.append(tf.variables_initializer([self.history_ptr]))
        return reset
